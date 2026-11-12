#!/usr/bin/env python3
"""Local, secret-free deployment registry and scoped DNS policy proposal.

No AWS calls, credentials creation, infrastructure mutation or cleanup execution.
Existing launch journals remain authoritative for actual resource identifiers.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import shlex
import uuid

FIELDS = {'version', 'id', 'account', 'region', 'ssh_key_path', 'aws_profile',
          'zone_id', 'domain', 'tls'}


def validate(value):
    if (not isinstance(value, dict) or value.get('version') not in (1, 2)
            or set(value) != (FIELDS | ({'group'} if value['version'] == 2 else set()))):
        raise ValueError('Unsupported deployment manifest fields/version')
    patterns = {'id': (r'enmaas-[a-f0-9]{16}' if value['version'] == 1 else r'em-[a-z][a-z0-9]{0,7}-[a-f0-9]{12}'), 'account': r'[0-9]{12}',
                'region': r'[a-z]{2}(?:-[a-z]+)+-[0-9]',
                'aws_profile': r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}'}
    for key, pattern in patterns.items():
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise ValueError('Invalid deployment field: ' + key)
    if value['version'] == 2:
        group = value['group']
        if not isinstance(group, str) or not re.fullmatch(r'[a-z][a-z0-9]{0,7}', group):
            raise ValueError('Group must be 1–8 lowercase letters/digits, starting with a letter')
        if not re.fullmatch('em-' + group + r'-[a-f0-9]{12}', value['id']):
            raise ValueError('Deployment ID does not match its group')
    key = value['ssh_key_path']
    if not isinstance(key, str) or not Path(key).is_absolute() or any(ord(c) < 32 for c in key):
        raise ValueError('SSH key must be an absolute path, not key contents')
    if value['tls'] != 'private-ca':
        raise ValueError('Only explicit private-CA trust is currently supported')
    zone, domain = value['zone_id'], value['domain']
    if not isinstance(zone, str) or not isinstance(domain, str) or bool(zone) != bool(domain):
        raise ValueError('Supply both hosted-zone ID and domain, or neither')
    if zone:
        if not re.fullmatch(r'Z[A-Z0-9]+', zone):
            raise ValueError('Invalid hosted-zone ID')
        labels = domain.split('.')
        if len(labels) < 2 or len(domain) > 190 or any(
                not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                for label in labels):
            raise ValueError('Use a lowercase domain without wildcards or trailing dot')
    return value


def new(account, region, ssh_key_path, aws_profile, zone_id='', domain='', *, group=None):
    value = {'version': 1, 'id': 'enmaas-' + uuid.uuid4().hex[:16],
             'account': account, 'region': region, 'ssh_key_path': ssh_key_path,
             'aws_profile': aws_profile, 'zone_id': zone_id, 'domain': domain,
             'tls': 'private-ca'}
    if group is not None:
        value.update(version=2, group=group, id='em-' + group + '-' + uuid.uuid4().hex[:12])
    return validate(value)


def save(root, value):
    validate(value)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Registry directory must be owned by this user and private (0700)')
    path = root / (value['id'] + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return path


def read_private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError('Cannot open deployment manifest safely') from error
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError('Manifest must be a private, user-owned regular file')
        return json.load(stream)




def load(path):
    return validate(read_private_json(path))

def shell_selection(value):
    validate(value)
    fields = {'RUN_PREFIX': value['id'], 'REGION': value['region'],
              'AWS_TEST_EXPECTED_ACCOUNT': value['account'],
              'SSH_KEY': value['ssh_key_path'], 'AWS_PROFILE': value['aws_profile']}
    if value['version'] == 2:
        fields['DEPLOYMENT_GROUP'] = value['group']
    # Selecting a deployment must not retain another account's static credentials
    # or a previously selected SSH target. The operator revalidates with discovery.
    clear = ('unset DEPLOYMENT_GROUP\n'
             'unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN\n'
             'unset AWS_DEFAULT_PROFILE AWS_TEST_READY AWS_TEST_PLAN_INPUTS AWS_TEST_CREDENTIALS ACCOUNT\n'
             'unset RHEL_HOST RHEL_SCENARIO RHEL_INFERENCE RHEL_VPC_ID RHEL_STATE_FILE\n')
    return clear + '\n'.join('export '+key+'='+shlex.quote(item) for key, item in fields.items())

def dns_policy(value):
    """Permission fragment only; no hosted-zone deletion, wildcard or UPSERT.

    CREATE refuses collisions. A future replacement must compare the journal's
    old record and use an atomic exact DELETE+CREATE batch, not blind UPSERT.
    """
    validate(value)
    statements = []
    if value['zone_id']:
        suffix = value['id'] + '.' + value['domain']
        names = [role + '.' + suffix for role in ('gateway', 'keycloak', 'observability')]
        names.append('_enmaas-owner.' + suffix)
        statements.append({
            'Effect': 'Allow', 'Action': 'route53:ChangeResourceRecordSets',
            'Resource': 'arn:aws:route53:::hostedzone/' + value['zone_id'],
            'Condition': {'ForAllValues:StringEquals': {
                'route53:ChangeResourceRecordSetsNormalizedRecordNames': names,
                'route53:ChangeResourceRecordSetsRecordTypes': ['A', 'TXT'],
                'route53:ChangeResourceRecordSetsActions': ['CREATE', 'DELETE']},
                'Null': {'route53:ChangeResourceRecordSetsNormalizedRecordNames': 'false',
                         'route53:ChangeResourceRecordSetsRecordTypes': 'false',
                         'route53:ChangeResourceRecordSetsActions': 'false'}}})
    return {'Version': '2012-10-17', 'Statement': statements}


def trust_policy(value, principal, allow_role=False):
    """Exact same-account principal proposal; no IAM mutation or principal discovery."""
    validate(value)
    match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):(user|role)/'
                         r'(?:[A-Za-z0-9_+=,.@-]+/)*[A-Za-z0-9_+=,.@-]+', principal)
    if not match or match[1] != value['account']:
        raise ValueError('Supply one exact same-account IAM user or role ARN; no root, wildcard or STS-session principals')
    statement = {'Effect': 'Allow', 'Principal': {'AWS': principal}, 'Action': 'sts:AssumeRole'}
    if match[2] == 'user':
        statement['Condition'] = {'Bool': {'aws:MultiFactorAuthPresent': 'true'}}
    elif not allow_role:
        raise ValueError('Role trust requires --allow-role-principal after reviewing source-role authentication; MFA is not asserted by this policy')
    return {'Version': '2012-10-17', 'Statement': [statement]}


def inspection_policy(value):
    """Reviewable read-only fragment; not a provisioning role or IAM bootstrap.

    EC2 Describe APIs expose regional metadata beyond the deployment. Do not
    mistake helper-side ownership filters for an IAM confidentiality boundary.
    """
    validate(value)
    region={'StringEquals':{'aws:RequestedRegion':value['region']}}
    base=f"arn:aws:rds:{value['region']}:{value['account']}:"
    name=value['id']+'-postgres'
    statements=[
        {'Sid':'CallerIdentity','Effect':'Allow','Action':['sts:GetCallerIdentity'],'Resource':'*'},
        {'Sid':'RegionalDiscovery','Effect':'Allow','Action':[
            'ec2:DescribeInstances','ec2:DescribeImages','ec2:DescribeInstanceTypes',
            'ec2:DescribeInstanceTypeOfferings','ec2:DescribeSubnets','ec2:DescribeRouteTables',
            'ec2:DescribeSecurityGroups','ec2:DescribeKeyPairs','ec2:DescribeVolumes',
            'ec2:DescribeNetworkInterfaces','ec2:DescribeVpcAttribute',
            'rds:DescribeDBEngineVersions','rds:DescribeOrderableDBInstanceOptions',
            'rds:DescribeDBSnapshots','rds:DescribeDBInstanceAutomatedBackups'],
         'Resource':'*','Condition':region},
        {'Sid':'DeploymentDatabase','Effect':'Allow','Action':['rds:DescribeDBInstances'],
         'Resource':base+'db:'+name,'Condition':region},
        {'Sid':'DeploymentParameters','Effect':'Allow',
         'Action':['rds:DescribeDBParameters','rds:DescribeDBParameterGroups'],
         'Resource':base+'pg:'+name,'Condition':region},
        {'Sid':'DeploymentDatabaseTags','Effect':'Allow','Action':['rds:ListTagsForResource'],
         'Resource':[base+kind+':'+name for kind in ('db','pg','subgrp')],'Condition':region},
    ]
    if value['zone_id']:
        statements.append({'Sid':'ApprovedZoneMetadata','Effect':'Allow',
            'Action':['route53:GetHostedZone','route53:ListResourceRecordSets'],
            'Resource':'arn:aws:route53:::hostedzone/'+value['zone_id']})
    return {'Version':'2012-10-17','Statement':statements}


def summary(value, journals):
    """Safe metadata projection; journal state is not a live AWS health check."""
    validate(value)
    result = {key: value.get(key) for key in
              ('id', 'group', 'account', 'region', 'aws_profile', 'ssh_key_path', 'tls', 'domain', 'zone_id')}
    result['source'] = 'Local recovery journals; run live verification before use or cleanup'
    result['vms'] = []
    fields = ('InstanceId', 'ApplyStatus', 'VpcId', 'SubnetId', 'AvailabilityZone',
              'InstanceType', 'VolumeGiB', 'SecurityGroupId', 'KeyPairName',
              'SshAccess', 'HttpsAccess', 'Ingress')
    for role, scenario in (('gateway', 'remote-gateway'), ('keycloak', 'keycloak'), ('observability', 'observability')):
        path = journals / (value['id'] + '-' + role + '.json')
        row = {'role': role, 'journal': str(path)}
        if path.is_symlink():
            raise ValueError('Recovery journal must not be a symbolic link')
        if path.exists():
            state = read_private_json(path)
            expected = {'AccountId': value['account'], 'Region': value['region'],
                        'Prefix': value['id']+'-'+role, 'Scenario': scenario}
            if not isinstance(state, dict) or any(state.get(k) != v for k, v in expected.items()):
                raise ValueError('Recovery journal belongs to a different deployment/account/role')
            row.update({key: state[key] for key in fields if key in state})
        else:
            row['ApplyStatus'] = 'No launch journal'
        result['vms'].append(row)
    result['cleanup'] = 'Read-only inventory/teardown plan available; deletion executor not implemented'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=Path('.state/deployments'))
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    for field in ('account', 'region', 'ssh-key-path', 'aws-profile'):
        init.add_argument('--' + field, required=True)
    init.add_argument('--group', required=True, help='shared operator group: 1–8 lowercase letters/digits, starting with a letter')
    init.add_argument('--zone-id', default='')
    init.add_argument('--domain', default='')
    commands.add_parser('list')
    for name in ('show', 'dns-policy', 'inspection-policy', 'env'):
        commands.add_parser(name).add_argument('manifest', type=Path)
    report = commands.add_parser('summary', help='print safe metadata from VM recovery journals')
    report.add_argument('manifest', type=Path)
    report.add_argument('--journals', type=Path, default=Path('.state'))
    trust = commands.add_parser('trust-policy', help='render an exact-principal trust proposal without creating a role')
    trust.add_argument('manifest', type=Path)
    trust.add_argument('--principal-arn', required=True)
    trust.add_argument('--allow-role-principal', action='store_true', help='acknowledge that source-role MFA/authentication must be enforced separately')
    args = parser.parse_args()
    if args.command == 'init':
        print(save(args.registry, new(args.account, args.region, args.ssh_key_path,
                                      args.aws_profile, args.zone_id, args.domain, group=args.group)))
    elif args.command == 'list':
        for path in sorted(args.registry.glob('*.json')):
            value = load(path)
            print(json.dumps({key: value.get(key) for key in ('id', 'group', 'account', 'region', 'domain', 'aws_profile')}))
    else:
        value = load(args.manifest)
        if args.command == 'summary':
            print(json.dumps(summary(value, args.journals), indent=2))
        elif args.command == 'env':
            print(shell_selection(value))
        elif args.command == 'trust-policy':
            print(json.dumps(trust_policy(value, args.principal_arn, args.allow_role_principal), indent=2))
        else:
            policy={'dns-policy':dns_policy,'inspection-policy':inspection_policy}.get(args.command)
            print(json.dumps(policy(value) if policy else value, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
