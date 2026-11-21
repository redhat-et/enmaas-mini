import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('cleanup', (Path(__file__).resolve().parents[1] / 'cleanup-inventory.py'))
cleanup = importlib.util.module_from_spec(spec); spec.loader.exec_module(cleanup)


class Inventory(unittest.TestCase):
    def test_inventory_refuses_recreated_key_pair_with_original_name(self):
        manifest,journals,resources=self.fixture()
        state=journals['gateway'];state['KeyPairId']='key-1234567890abcdef0'
        key=resources['keys'][state['KeyPairName']]
        for identifier in (None,'key-abcdef12345678900','key-1234567890abcdef0'):
            key['KeyPairId']=identifier
            with self.subTest(identifier=identifier):
                if identifier==state['KeyPairId']:
                    cleanup.inventory(manifest,journals,resources)
                else:
                    with self.assertRaisesRegex(ValueError,'key-pair identity'):
                        cleanup.inventory(manifest,journals,resources)

    def test_group_references_report_owned_and_unowned_rules_without_adoption(self):
        def group(identifier,field):
            return {'GroupId':identifier,'OwnerId':'123456789012',field:[{
                'IpProtocol':'tcp','FromPort':8443,'ToPort':8443,
                'UserIdGroupPairs':[{'GroupId':'sg-target','UserId':'123456789012'}]}]}
        aws=Mock();aws.call.side_effect=[{'SecurityGroups':[group('sg-owned','IpPermissions')]},
                                         {'SecurityGroups':[group('sg-foreign','IpPermissionsEgress')]}]
        refs=cleanup.group_references(aws,'123456789012',{'sg-target','sg-owned'})
        self.assertEqual([row['direction'] for row in refs],['ingress','egress'])
        self.assertEqual([row['source_owned'] for row in refs],[True,False])
        self.assertTrue(all(row['deletion_authorized'] is False for row in refs))
        self.assertEqual([call.kwargs['filters'][0]['Name'] for call in aws.call.call_args_list],
                         ['ip-permission.group-id','egress.ip-permission.group-id'])

    def test_group_reference_inventory_rejects_partial_foreign_or_unmatched_results(self):
        group={'GroupId':'sg-source','OwnerId':'123456789012','IpPermissions':[{
            'IpProtocol':'-1','UserIdGroupPairs':[{'GroupId':'sg-target','UserId':'123456789012'}]}]}
        for mode in ('page','account','target-account','unmatched','duplicate'):
            row=copy.deepcopy(group)
            if mode=='account':row['OwnerId']='999999999999'
            if mode=='target-account':row['IpPermissions'][0]['UserIdGroupPairs'][0]['UserId']='999999999999'
            if mode=='unmatched':row['IpPermissions'][0]['UserIdGroupPairs'][0]['GroupId']='sg-other'
            response={'SecurityGroups':[row,row] if mode=='duplicate' else [row]}
            if mode=='page':response['NextToken']='unconsumed'
            aws=Mock();aws.call.return_value=response
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                cleanup.group_references(aws,'123456789012',{'sg-target'})

    def test_teardown_plan_preserves_data_and_never_deletes_interfaces_directly(self):
        manifest,journals,resources=self.fixture()
        rows=cleanup.inventory(manifest,journals,resources)
        dependencies=[{'id':'eni-foreign','relationship':'unresolved','deletion_authorized':False}]
        plan=cleanup.teardown_plan(rows,dependencies,None)
        self.assertFalse(plan['execution_enabled'])
        self.assertIn('eni-foreign',plan['unresolved_interfaces'])
        self.assertEqual(plan['stages'][1]['instances'],['i-0','i-1','i-2'])
        self.assertEqual(plan['stages'][1]['root_volumes_deleted_with_instances'],['vol-0','vol-1','vol-2'])
        self.assertEqual(plan['stages'][-1]['stage'],'local-state')
        self.assertEqual(plan['stages'][-1]['automatic_deletion'],False)

    def test_teardown_keeps_dedicated_vpc_as_unverified_final_dependency(self):
        recorded={'state':'uncertain-operation','confirmed_resources':[{'action':'create-vpc','id':'vpc-12345678'}],
                  'pending_operation':{'action':'create-subnet'},'live_verified':False,'deletion_authorized':False}
        plan=cleanup.teardown_plan([],[],None,network=recorded)
        step=next(row for row in plan['stages'] if row['stage']=='dedicated-vpc')
        self.assertEqual(step['creation_records'],recorded)
        self.assertFalse(step['deletion_authorized'])
        self.assertIn('live VPC',step['required'])
        self.assertLess(plan['stages'].index(step),len(plan['stages'])-1)
        absent=cleanup.teardown_plan([],[],None)
        self.assertIsNone(next(row for row in absent['stages'] if row['stage']=='dedicated-vpc')['creation_records'])

    def test_rds_plan_requires_retention_choice_and_keeps_managed_secret_service_owned(self):
        manifest,journals,resources=self.fixture()
        rows=cleanup.inventory(manifest,journals,resources)
        database={'resources':[{'kind':'database','id':'arn:db','resource_id':'db-ORIGINAL'},
                              {'kind':'managed_secret_reference','id':'arn:secret'}],
                  'retained_storage':[{'kind':'snapshot','id':'arn:snapshot'}]}
        plan=cleanup.teardown_plan(rows,[],database)
        step=next(row for row in plan['stages'] if row['stage']=='database')
        self.assertEqual(step['resources'],[database['resources'][0]])
        self.assertTrue(step['retention_choice_required'])
        self.assertEqual(step['retained_storage'],database['retained_storage'])
        self.assertEqual(step['independent_managed_secret_deletion'],False)

    def test_retained_storage_is_scoped_to_immutable_database_and_never_authorizes_deletion(self):
        manifest,_,_=self.fixture()
        name=manifest['id']+'-postgres'
        arn=f"arn:aws:rds:{manifest['region']}:{manifest['account']}:snapshot:retained-test"
        snapshot={'DbiResourceId':'db-ORIGINAL','DBInstanceIdentifier':name,
                  'DBSnapshotArn':arn,'SnapshotType':'manual','Status':'available','Encrypted':True}
        backup={'DbiResourceId':'db-ORIGINAL','DBInstanceIdentifier':name,
                'Status':'active','Encrypted':True}
        aws=Mock();aws.call.side_effect=[{'DBSnapshots':[snapshot]}, {'DBInstanceAutomatedBackups':[backup]}]
        rows=cleanup.retained_storage(manifest,'db-ORIGINAL',aws)
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['deletion_authorized'] is False for row in rows))
        self.assertEqual(rows[0]['id'],arn)
        self.assertEqual(rows[1]['resource_id'],'db-ORIGINAL')
        self.assertTrue(all(call.kwargs=={'dbi_resource_id':'db-ORIGINAL'} for call in aws.call.call_args_list))
        for change in ('identity','account','duplicate','page'):
            changed=copy.deepcopy(snapshot)
            if change=='identity':changed['DbiResourceId']='db-REPLACEMENT'
            if change=='account':changed['DBSnapshotArn']=arn.replace(manifest['account'],'999999999999')
            response={'DBSnapshots':[changed,changed] if change=='duplicate' else [changed]}
            if change=='page':response['Marker']='remaining'
            aws=Mock();aws.call.side_effect=[response,{'DBInstanceAutomatedBackups':[]}]
            with self.subTest(change=change),self.assertRaises(ValueError):
                cleanup.retained_storage(manifest,'db-ORIGINAL',aws)

    def fixture(self):
        manifest = cleanup.registry.new('123456789012', 'eu-central-1', '/tmp/key', 'lab')
        journals = {}; resources = {kind: {} for kind in ('instances', 'groups', 'keys', 'volumes')}
        for index, (role, scenario) in enumerate(cleanup.ROLES.items()):
            prefix = manifest['id']+'-'+role
            instance, group, volume = 'i-'+str(index), 'sg-'+str(index), 'vol-'+str(index)
            journals[role] = {'AccountId': manifest['account'], 'Region': manifest['region'],
                              'Prefix': prefix, 'Scenario': scenario, 'InstanceId': instance,
                              'SecurityGroupId': group, 'KeyPairName': prefix, 'RootDeviceName': '/dev/sda1'}
            tags = cleanup.vm.tags(prefix, scenario)
            resources['instances'][instance] = {'Tags': tags, 'KeyName': prefix,
                'SecurityGroups': [{'GroupId': group}], 'BlockDeviceMappings': [
                    {'DeviceName': '/dev/sda1', 'Ebs': {'VolumeId': volume, 'DeleteOnTermination': True}}]}
            resources['groups'][group] = {'Tags': tags, 'OwnerId': manifest['account']}
            resources['keys'][prefix] = {'Tags': tags}
            resources['volumes'][volume] = {'Tags': tags, 'Attachments': [{'InstanceId': instance}]}
        return manifest, journals, resources

    def test_live_resource_collection_rejects_partial_and_duplicate_results(self):
        manifest,_,resources=self.fixture()
        responses=[{'Reservations':[{'OwnerId':manifest['account'],'Instances':[
            {'InstanceId':key,**value} for key,value in resources['instances'].items()]}]}]
        for kind,field,key in (('groups','SecurityGroups','GroupId'),('keys','KeyPairs','KeyName'),
                               ('volumes','Volumes','VolumeId')):
            responses.append({field:[{key:identifier,**row} for identifier,row in resources[kind].items()]})
        aws=Mock();aws.call.side_effect=copy.deepcopy(responses)
        result=cleanup.collect_resources(aws,manifest)
        self.assertEqual(set(result),set(resources))
        for index,field in enumerate(('Reservations','SecurityGroups','KeyPairs','Volumes')):
            for change in ('partial','duplicate','empty-id'):
                altered=copy.deepcopy(responses)
                if change=='partial':altered[index]['NextToken']='more'
                else:
                    rows=altered[index][field]
                    if index==0:rows=rows[0]['Instances']
                    if change=='duplicate':rows.append(copy.deepcopy(rows[0]))
                    else:rows[0][('InstanceId','GroupId','KeyName','VolumeId')[index]]=''
                aws=Mock();aws.call.side_effect=altered
                with self.subTest(index=index,change=change),self.assertRaises(ValueError):
                    cleanup.collect_resources(aws,manifest)

    def test_exact_inventory(self):
        result = cleanup.inventory(*self.fixture())
        self.assertEqual(len(result), 12)
        self.assertNotIn('password', str(result))
        self.assertEqual(sum(item.get('delete_with_instance', False) for item in result), 3)

    def test_foreign_missing_or_extra_resources_rejected(self):
        for kind in ('instances', 'groups', 'keys', 'volumes'):
            for change in ('missing', 'untagged', 'extra'):
                with self.subTest(kind=kind, change=change):
                    manifest, journals, resources = self.fixture()
                    identifier = next(iter(resources[kind]))
                    if change == 'missing':del resources[kind][identifier]
                    elif change == 'untagged':resources[kind][identifier]['Tags'] = []
                    else:resources[kind]['foreign-extra'] = copy.deepcopy(resources[kind][identifier])
                    with self.assertRaises(ValueError):cleanup.inventory(manifest, journals, resources)

    def test_changed_journal_or_attachment_rejected(self):
        for change in ('account', 'prefix', 'attachment', 'retained', 'group'):
            manifest, journals, resources = self.fixture()
            if change == 'account':journals['gateway']['AccountId'] = '999999999999'
            elif change == 'prefix':journals['gateway']['Prefix'] = 'foreign'
            elif change == 'attachment':resources['volumes']['vol-0']['Attachments'] = [{'InstanceId': 'foreign'}]
            elif change == 'retained':resources['instances']['i-0']['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination'] = False
            else:resources['instances']['i-0']['SecurityGroups'].append({'GroupId': 'foreign'})
            with self.subTest(change=change), self.assertRaises(ValueError):
                cleanup.inventory(manifest, journals, resources)

    def test_network_dependencies_are_reported_without_adopting_them(self):
        rows = [
            {'NetworkInterfaceId':'eni-vm','OwnerId':'123456789012',
             'Groups':[{'GroupId':'sg-0'}], 'RequesterManaged':False,
             'Attachment':{'InstanceId':'i-0','DeleteOnTermination':True}},
            {'NetworkInterfaceId':'eni-service','OwnerId':'123456789012',
             'Groups':[{'GroupId':'sg-rds'}], 'RequesterManaged':True},
            {'NetworkInterfaceId':'eni-foreign','OwnerId':'123456789012',
             'Groups':[{'GroupId':'sg-0'},{'GroupId':'sg-other'}],
             'RequesterManaged':False,'Attachment':{'InstanceId':'i-foreign'}}]
        aws = Mock();aws.call.return_value = {'NetworkInterfaces':rows}
        result=cleanup.network_dependencies(aws, '123456789012', {'sg-0','sg-rds'}, {'i-0'})
        self.assertEqual([r['relationship'] for r in result],
                         ['journaled VM', 'service-managed; reconcile with owning service', 'unresolved'])
        self.assertTrue(all(r['deletion_authorized'] is False for r in result))
        self.assertEqual(aws.call.call_args.args, ('ec2','describe-network-interfaces'))
        self.assertEqual(aws.call.call_args.kwargs['filters'],
                         [{'Name':'group-id','Values':['sg-0','sg-rds']}])
        self.assertNotIn('PrivateIpAddress',str(result))

    def test_network_dependency_query_rejects_wrong_scope_and_incomplete_results(self):
        base={'NetworkInterfaceId':'eni-test','OwnerId':'123456789012',
              'Groups':[{'GroupId':'sg-0'}]}
        responses=[{'NetworkInterfaces':[{**base,'OwnerId':'999999999999'}]},
                   {'NetworkInterfaces':[{**base,'Groups':[{'GroupId':'sg-other'}]}]},
                   {'NetworkInterfaces':[base,base]},
                   {'NetworkInterfaces':[base],'NextToken':'unconsumed'}]
        for response in responses:
            with self.subTest(response=response),self.assertRaises(ValueError):
                aws=Mock();aws.call.return_value=response
                cleanup.network_dependencies(aws,'123456789012',{'sg-0'},{'i-0'})

    def test_rds_inventory_requires_same_deployment_and_successful_verifier(self):
        manifest, _, _ = self.fixture()
        journal = {'Plan': {'AccountId': manifest['account'], 'Region': manifest['region'],
                           'Prefix': manifest['id']},
                   'Resources': {'SecurityGroup': 'sg-rds', 'SubnetGroup': 'arn:subnet',
                                 'ParameterGroup': 'arn:parameter',
                                 'Instance': {'Arn': 'arn:database', 'ResourceId':'db-ORIGINAL', 'MasterSecretArn': 'arn:secret'}}}
        with patch.object(cleanup.rds, 'verify', return_value={'Ready': True}) as verify, \
                patch.object(cleanup,'retained_storage',return_value=[]) as backups:
            result = cleanup.rds_inventory(manifest, journal, Mock())
            verify.assert_called_once()
            self.assertEqual(len(result['resources']), 5)
            self.assertEqual(result['resources'][0]['resource_id'],'db-ORIGINAL')
            self.assertFalse(result['deletion_enabled'])
            backups.assert_called_once()
        foreign = copy.deepcopy(journal); foreign['Plan']['Prefix'] = 'another-run'
        with patch.object(cleanup.rds, 'verify') as verify, self.assertRaises(ValueError):
            cleanup.rds_inventory(manifest, foreign, Mock())
        verify.assert_not_called()
        with patch.object(cleanup.rds, 'verify', side_effect=ValueError('tags differ')):
            with self.assertRaises(ValueError):cleanup.rds_inventory(manifest, journal, Mock())

    def test_cli_inventory_reads_exact_scope_and_never_enables_deletion(self):
        for include_plan in (False, True):
            manifest,journals,resources=self.fixture()
            for kind,key in (('instances','InstanceId'),('groups','GroupId'),('keys','KeyName'),('volumes','VolumeId')):
                for identifier,row in resources[kind].items():row[key]=identifier
            aws=Mock()
            results={'describe-security-groups':{'SecurityGroups':list(resources['groups'].values())},
                     'describe-key-pairs':{'KeyPairs':list(resources['keys'].values())},
                     'describe-volumes':{'Volumes':list(resources['volumes'].values())},
                     'describe-instances':{'Reservations':[{'OwnerId':manifest['account'],'Instances':list(resources['instances'].values())}]}}
            def call(service,action,**options):
                self.assertEqual(service,'ec2')
                self.assertEqual(options['filters'],[{'Name':'tag:enmaas:run','Values':[manifest['id']]}])
                return results[action]
            aws.call.side_effect=call
            def journal(path):
                return journals[next(role for role in journals if str(path).endswith('-'+role+'.json'))]
            output=io.StringIO()
            argv=['cleanup-inventory.py','manifest.json']+(['--teardown-plan','--network-journal','network'] if include_plan else [])
            with self.subTest(plan=include_plan),patch.object(cleanup.network_journal,'inspect',return_value={'live_verified':False}) as network,patch('sys.argv',argv),patch.object(cleanup.registry,'load',return_value=manifest),patch.object(cleanup.registry,'read_private_json',side_effect=journal),patch.object(cleanup.vm,'Aws',return_value=aws) as client,patch.object(cleanup.vm,'check_identity') as identity,patch.object(cleanup,'network_dependencies',return_value=[]),patch.object(cleanup,'group_references',return_value=[]),contextlib.redirect_stdout(output):
                cleanup.main()
                client.assert_called_once_with(manifest['region'],manifest['aws_profile'],False)
                identity.assert_called_once_with(aws,manifest['account'])
                report=json.loads(output.getvalue())
                self.assertFalse(report['deletion_enabled'])
                self.assertEqual(len(report['ec2_inventory']),12)
                self.assertEqual('teardown_plan' in report,include_plan)
                if include_plan:
                    self.assertFalse(report['teardown_plan']['execution_enabled'])
                    network.assert_called_once_with(Path('network'),manifest)
                    self.assertEqual(report['network_creation_records'],{'live_verified':False})
                else:
                    network.assert_not_called()
                    self.assertIsNone(report['network_creation_records'])
                results['describe-instances']['Reservations'][0]['OwnerId']='999999999999'
                aws.call.reset_mock()
                with self.assertRaisesRegex(ValueError,'reservation account'):
                    cleanup.main()
                self.assertEqual(aws.call.call_count,1)

    def test_invalid_network_journal_stops_before_aws_inventory(self):
        manifest,_,_=self.fixture()
        with patch('sys.argv',['cleanup-inventory.py','manifest.json','--network-journal','network']), \
             patch.object(cleanup.registry,'load',return_value=manifest), \
             patch.object(cleanup.network_journal,'inspect',side_effect=ValueError('foreign journal')), \
             patch.object(cleanup.vm,'Aws') as client,self.assertRaises(ValueError):
            cleanup.main()
        client.assert_not_called()

    def test_cli_absent_manifest_fails_without_cloud_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,str((Path(__file__).resolve().parents[1] / 'cleanup-inventory.py')),
                str(Path(tmp)/'missing')],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Inventory stopped; no resources changed:',result.stderr)

    def test_inventory_rejects_shared_resources_wrong_account_and_extra_disks(self):
        for case in ('shared','account','disks'):
            manifest,journals,resources=self.fixture()
            if case=='shared':journals['keycloak']['SecurityGroupId']=journals['gateway']['SecurityGroupId']
            if case=='account':resources['groups']['sg-0']['OwnerId']='999999999999'
            if case=='disks':resources['instances']['i-0']['BlockDeviceMappings'].append({'DeviceName':'/dev/sdb'})
            # The duplicate-ID guard is independent of tag validation (covered
            # separately); emulate a resource already accepted by that validator.
            with self.subTest(case=case),patch.object(cleanup.vm,'owned_tags',return_value=True):
                with self.assertRaisesRegex(ValueError,{'shared':'shared across roles','account':'account differs','disks':'unexpected disks'}[case]):
                    cleanup.inventory(manifest,journals,resources)

    def test_dependency_queries_require_explicit_scope(self):
        aws=Mock()
        with self.assertRaisesRegex(ValueError,'explicit deployment'):
            cleanup.network_dependencies(aws,'123456789012',set(),set())
        with self.assertRaisesRegex(ValueError,'explicit deployment'):
            cleanup.group_references(aws,'123456789012',set())
        manifest,_,_=self.fixture()
        with self.assertRaisesRegex(ValueError,'immutable database ID'):
            cleanup.retained_storage(manifest,'wrong',aws)
        aws.call.assert_not_called()

    def test_cli_rds_group_is_separate_and_remains_in_dependency_scope(self):
        manifest,journals,resources=self.fixture()
        for kind,key in (('instances','InstanceId'),('groups','GroupId'),('keys','KeyName'),('volumes','VolumeId')):
            for identifier,row in resources[kind].items():row[key]=identifier
        rds_journal={'Resources':{'SecurityGroup':'sg-rds'}}
        database={'resources':[],'retained_storage':[]}
        for mode in ('valid','missing','shared'):
            groups=list(resources['groups'].values())
            if mode=='valid':groups.append({'GroupId':'sg-rds'})
            rds_journal['Resources']['SecurityGroup']='sg-0' if mode=='shared' else 'sg-rds'
            results={'describe-instances':{'Reservations':[{'OwnerId':manifest['account'],'Instances':list(resources['instances'].values())}]},
                     'describe-security-groups':{'SecurityGroups':groups},
                     'describe-key-pairs':{'KeyPairs':list(resources['keys'].values())},
                     'describe-volumes':{'Volumes':list(resources['volumes'].values())}}
            aws=Mock();aws.call.side_effect=lambda service,action,**kwargs:results[action]
            def journal(path):
                if str(path)=='rds.json':return rds_journal
                return journals[next(role for role in journals if str(path).endswith('-'+role+'.json'))]
            with self.subTest(mode=mode),patch('sys.argv',['cleanup-inventory.py','manifest.json','--rds-journal','rds.json']),patch.object(cleanup.registry,'load',return_value=manifest),patch.object(cleanup.registry,'read_private_json',side_effect=journal),patch.object(cleanup.vm,'Aws',return_value=aws),patch.object(cleanup.vm,'check_identity'),patch.object(cleanup.rds,'ReadOnlyAws'),patch.object(cleanup,'rds_inventory',return_value=database),patch.object(cleanup,'network_dependencies',return_value=[]) as dependencies,patch.object(cleanup,'group_references',return_value=[]),contextlib.redirect_stdout(io.StringIO()):
                if mode=='valid':
                    cleanup.main()
                    self.assertIn('sg-rds',dependencies.call_args.args[2])
                else:
                    with self.assertRaisesRegex(ValueError,'missing or unexpectedly shared'):
                        cleanup.main()
                    dependencies.assert_not_called()

if __name__ == '__main__':unittest.main()
