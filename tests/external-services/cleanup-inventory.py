#!/usr/bin/env python3
"""Read-only EC2 cleanup inventory; never deletes resources or reads secret values.

This does not qualify deletion, DNS/IAM cleanup or orphan reconciliation. A successful
inventory is not authorization to delete resources.
"""
import argparse
import importlib.util
import importlib.machinery
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('deployments', HERE/'deployments.py')
registry = importlib.util.module_from_spec(spec); spec.loader.exec_module(registry)
loader = importlib.machinery.SourceFileLoader('cleanup_vm', str(HERE.parents[1]/'scripts/aws/rhel-vm'))
spec = importlib.util.spec_from_loader(loader.name, loader)
vm = importlib.util.module_from_spec(spec); loader.exec_module(vm)
spec = importlib.util.spec_from_file_location('cleanup_rds', HERE/'rds.py')
rds = importlib.util.module_from_spec(spec); spec.loader.exec_module(rds)
ROLES = {'gateway': 'remote-gateway', 'keycloak': 'keycloak', 'observability': 'observability'}


def inventory(manifest, journals, resources):
    registry.validate(manifest)
    rows = []
    consumed = {kind: set() for kind in resources}
    for role, scenario in ROLES.items():
        state = journals[role]
        prefix = manifest['id']+'-'+role
        for key, expected in {'AccountId': manifest['account'], 'Region': manifest['region'],
                              'Prefix': prefix, 'Scenario': scenario}.items():
            if state.get(key) != expected:
                raise ValueError('journal differs from deployment: '+role+'/'+key)
        for kind, field in (('instances', 'InstanceId'), ('groups', 'SecurityGroupId'), ('keys', 'KeyPairName')):
            identifier = state.get(field)
            resource = resources[kind].get(identifier)
            if not identifier or resource is None or not vm.owned_tags(resource, prefix, scenario):
                raise ValueError('missing or foreign journal resource: '+role+'/'+kind)
            if identifier in consumed[kind]:
                raise ValueError('resource shared across roles')
            if kind == 'groups' and resource.get('OwnerId') != manifest['account']:
                raise ValueError('security group account differs')
            consumed[kind].add(identifier)
            rows.append({'role': role, 'kind': kind, 'id': identifier})
        instance = resources['instances'][state['InstanceId']]
        if ([group['GroupId'] for group in instance.get('SecurityGroups', [])] != [state['SecurityGroupId']]
                or instance.get('KeyName') != state['KeyPairName']):
            raise ValueError('instance security-group or key association differs')
        mappings = instance.get('BlockDeviceMappings', [])
        if len(mappings) != 1 or mappings[0].get('DeviceName') != state.get('RootDeviceName'):
            raise ValueError('unexpected disks; manual reconciliation required')
        disk = mappings[0]['Ebs']; identifier = disk['VolumeId']
        volume = resources['volumes'].get(identifier)
        if (volume is None or not vm.owned_tags(volume, prefix, scenario)
                or not disk.get('DeleteOnTermination')
                or {a['InstanceId'] for a in volume.get('Attachments', [])} != {state['InstanceId']}
                or identifier in consumed['volumes']):
            raise ValueError('root volume ownership, attachment or deletion policy differs')
        consumed['volumes'].add(identifier)
        rows.append({'role': role, 'kind': 'volumes', 'id': identifier, 'delete_with_instance': True})
    for kind in consumed:
        if set(resources[kind]) != consumed[kind]:
            raise ValueError('unjournaled deployment-tagged resources require reconciliation: '+kind)
    return rows



def network_dependencies(aws, account, groups, instances):
    """Report SG users, including untagged/service-managed ENIs; never adopt them."""
    if not groups:
        raise ValueError('explicit deployment security groups required')
    response = aws.call('ec2', 'describe-network-interfaces',
                        filters=[{'Name': 'group-id', 'Values': sorted(groups)}])
    # AWS CLI normally aggregates every page. Never report a partial inventory.
    if response.get('NextToken'):
        raise ValueError('network-interface inventory is incomplete')
    result = []; seen = set()
    for item in response['NetworkInterfaces']:
        identifier = item['NetworkInterfaceId']
        associated = {group['GroupId'] for group in item['Groups']}
        if (item['OwnerId'] != account or not associated.intersection(groups)
                or not identifier or identifier in seen):
            raise ValueError('network-interface ownership, query scope or uniqueness differs')
        seen.add(identifier)
        attachment = item.get('Attachment', {})
        instance = attachment.get('InstanceId')
        relationship = 'unresolved'
        if item.get('RequesterManaged') is True:
            relationship = 'service-managed; reconcile with owning service'
        elif (item.get('RequesterManaged') is False and instance in instances
              and associated.issubset(groups)):
            relationship = 'journaled VM'
        result.append({'id': identifier, 'security_groups': sorted(associated),
                       'instance': instance, 'relationship': relationship,
                       'delete_with_instance': attachment.get('DeleteOnTermination') is True,
                       'deletion_authorized': False})
    return result


def group_references(aws, account, groups):
    """Read current-account inbound/outbound references, including unowned SGs."""
    if not groups:
        raise ValueError('explicit deployment security groups required')
    rows = []
    for direction, field, filter_name in (
            ('ingress', 'IpPermissions', 'ip-permission.group-id'),
            ('egress', 'IpPermissionsEgress', 'egress.ip-permission.group-id')):
        response = aws.call('ec2', 'describe-security-groups',
                            filters=[{'Name': filter_name, 'Values': sorted(groups)}])
        if response.get('NextToken'):
            raise ValueError('security-group reference inventory is incomplete')
        seen = set()
        for group in response['SecurityGroups']:
            source = group['GroupId']
            if not source or source in seen or group.get('OwnerId') != account:
                raise ValueError('referencing security-group account or uniqueness differs')
            seen.add(source)
            matched = False
            for rule in group.get(field, []):
                for target in rule.get('UserIdGroupPairs', []):
                    if target.get('GroupId') not in groups:
                        continue
                    if target.get('UserId') != account:
                        raise ValueError('referenced security-group account differs')
                    matched = True
                    rows.append({'source_group': source, 'target_group': target['GroupId'],
                                 'direction': direction, 'protocol': rule['IpProtocol'],
                                 'from_port': rule.get('FromPort'), 'to_port': rule.get('ToPort'),
                                 'source_owned': source in groups, 'deletion_authorized': False})
            if not matched:
                raise ValueError('security-group reference query scope differs')
    return rows


def retained_storage(manifest, resource_id, aws):
    """List current-region RDS backups for the original database; no adoption."""
    registry.validate(manifest)
    if not isinstance(resource_id,str) or not re.fullmatch(r'db-[A-Za-z0-9]+',resource_id):
        raise ValueError('recorded immutable database ID required for backup inventory')
    base=f"arn:aws:rds:{manifest['region']}:{manifest['account']}:"
    result=[]
    for action,field,kind,arn_field,arn_kind in (
        ('describe-db-snapshots','DBSnapshots','snapshot','DBSnapshotArn','snapshot'),
        ('describe-db-instance-automated-backups','DBInstanceAutomatedBackups','automated-backup',
         'DBInstanceAutomatedBackupsArn','auto-backup'),
    ):
        response=aws.call('rds',action,dbi_resource_id=resource_id)
        if response.get('Marker') or response.get('NextToken'):
            raise ValueError('backup inventory is incomplete')
        seen=set()
        for row in response[field]:
            if (row.get('DbiResourceId')!=resource_id or
                    row.get('DBInstanceIdentifier')!=manifest['id']+'-postgres'):
                raise ValueError('backup source database differs')
            arn=row.get(arn_field)
            if (kind=='snapshot' and not arn) or (arn and (
                    not isinstance(arn,str) or not arn.startswith(base+arn_kind+':')
                    or not arn.removeprefix(base+arn_kind+':'))):
                raise ValueError('backup account, region or ARN differs')
            # Active automated backups need not have a separate replicated-backup ARN.
            identity=arn or resource_id
            if identity in seen:raise ValueError('duplicate backup identity')
            seen.add(identity)
            result.append({'kind':kind,'id':arn,'resource_id':resource_id,
                'state':row.get('Status'),'snapshot_type':row.get('SnapshotType'),
                'encrypted':row.get('Encrypted'),'deletion_authorized':False})
    return result


def rds_inventory(manifest, journal, aws):
    registry.validate(manifest)
    for key, expected in {'AccountId': manifest['account'], 'Region': manifest['region'],
                          'Prefix': manifest['id']}.items():
        if journal['Plan'].get(key) != expected:
            raise ValueError('RDS journal differs from selected deployment: '+key)
    verified = rds.verify(aws, journal, manifest['account'], manifest['region'])
    recorded = journal['Resources']
    return {'ready': verified['Ready'], 'deletion_enabled': False,
        'retained_storage':retained_storage(manifest,recorded['Instance']['ResourceId'],aws),
        'resources': [
        {'kind': 'database', 'id': recorded['Instance']['Arn'],
         'resource_id': recorded['Instance']['ResourceId']},
        {'kind': 'subnet_group', 'id': recorded['SubnetGroup']},
        {'kind': 'parameter_group', 'id': recorded['ParameterGroup']},
        {'kind': 'security_group', 'id': recorded['SecurityGroup']},
        {'kind': 'managed_secret_reference', 'id': recorded['Instance']['MasterSecretArn']}],
        'retention_decision_required': ['final snapshot', 'automated backups'],
        'note': 'Managed-secret reference only; no secret read or independent deletion authorized.'}

def teardown_plan(rows, dependencies, database, references=None):
    """Order manual review from live-verified inventory; never authorize deletion."""
    def ids(kind):
        return sorted(row['id'] for row in rows if row['kind'] == kind)
    unresolved = sorted(row['id'] for row in dependencies
                        if row['relationship'] != 'journaled VM' or not row.get('delete_with_instance'))
    stages = [
        {'stage': 'preserve-and-quiesce',
         'required': ['stop load generation and application writes',
                      'export required evidence and application/identity data',
                      'review DNS ownership and remove only exact journaled records',
                      'revalidate live resource ownership immediately before each mutation']},
        {'stage': 'instances', 'instances': ids('instances'),
         'root_volumes_deleted_with_instances': ids('volumes'),
         'required': 'explicitly approve loss of local application, identity and telemetry data; then wait for termination'},
    ]
    if database:
        stages.append({'stage': 'database',
                       'resources': [row for row in database['resources'] if row['kind'] == 'database'],
                       'retention_choice_required': True,
                       'retained_storage': database['retained_storage'],
                       'required': 'choose final snapshot and automated-backup retention; review deletion protection; wait for database deletion',
                       'independent_managed_secret_deletion': False})
    stages += [
        {'stage': 'network-and-database-dependencies', 'security_groups': ids('groups'),
         'database_dependencies': [row for row in (database or {}).get('resources', [])
                                   if row['kind'] in ('subnet_group', 'parameter_group', 'security_group')],
         'required': 'reinspect interfaces and security-group references after service deletion; never delete unowned interfaces or edit foreign groups'},
        {'stage': 'imported-key-pairs', 'key_pairs': ids('keys'),
         'required': 'confirm no surviving instances depend on these imported public keys'},
        {'stage': 'local-state', 'automatic_deletion': False,
         'required': 'retain journals and keys until cloud cleanup and retained-data choices are reconciled; remove local browser trust separately'},
    ]
    return {'execution_enabled': False, 'stages': stages,
            'unresolved_interfaces': unresolved,
            'security_group_references': references,
            'unverified': ['DNS and IAM ownership/dependencies', 'cross-account/peered-VPC security-group references',
                           'cross-region/AWS Backup recovery points', 'partial-launch recovery'],
            'note': 'Review plan only. No shell deletion commands or deletion authorization are produced.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--rds-journal', type=Path, help='optional private RDS creation journal')
    parser.add_argument('--state-dir', type=Path, default=Path('.state'))
    parser.add_argument('--teardown-plan', action='store_true', help='include an ordered manual review plan; performs no deletion')
    args = parser.parse_args()
    manifest = registry.load(args.manifest)
    journals = {role: registry.read_private_json(args.state_dir/(manifest['id']+'-'+role+'.json'))
                for role in ROLES}
    aws = vm.Aws(manifest['region'], manifest['aws_profile'], False)
    vm.check_identity(aws, manifest['account'])
    filters = [{'Name': 'tag:enmaas:run', 'Values': [manifest['id']]}]
    reservations = aws.call('ec2', 'describe-instances', filters=filters)['Reservations']
    if any(row.get('OwnerId') != manifest['account'] for row in reservations):
        raise ValueError('instance reservation account differs')
    resources = {'instances': {item['InstanceId']: item for row in reservations for item in row['Instances']}}
    for kind, action, result, key in (
            ('groups', 'describe-security-groups', 'SecurityGroups', 'GroupId'),
            ('keys', 'describe-key-pairs', 'KeyPairs', 'KeyName'),
            ('volumes', 'describe-volumes', 'Volumes', 'VolumeId')):
        resources[kind] = {item[key]: item for item in aws.call('ec2', action, filters=filters)[result]}
    dependency_groups = set(resources['groups'])
    database = None
    if args.rds_journal:
        journal = registry.read_private_json(args.rds_journal)
        database = rds_inventory(manifest, journal, rds.ReadOnlyAws(manifest['region'], manifest['aws_profile']))
        group = journal['Resources']['SecurityGroup']
        if group not in resources['groups'] or any(row.get('SecurityGroupId') == group for row in journals.values()):
            raise ValueError('RDS group is missing or unexpectedly shared with a VM')
        del resources['groups'][group]
    ec2_inventory = inventory(manifest, journals, resources)
    dependencies = network_dependencies(aws, manifest['account'], dependency_groups, set(resources['instances']))
    references = group_references(aws, manifest['account'], dependency_groups)
    result = {'deployment': manifest['id'], 'account': manifest['account'],
                      'region': manifest['region'], 'ec2_inventory': ec2_inventory,
                      'network_dependencies': dependencies,
                      'security_group_references': references,
                      'rds_inventory': database, 'deletion_enabled': False,
                      'remaining': ['backup retention decisions and cross-region/AWS Backup inventory', 'DNS and IAM inventory', 'network dependency reconciliation',
                                    'partial-launch/orphan reconciliation', 'live IAM deny tests']}
    if args.teardown_plan:
        result['teardown_plan'] = teardown_plan(ec2_inventory, dependencies, database, references)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit('Inventory stopped; no resources changed: '+str(error))
