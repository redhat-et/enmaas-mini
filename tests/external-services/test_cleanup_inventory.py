import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).with_name('cleanup-inventory.py'))
cleanup = importlib.util.module_from_spec(spec); spec.loader.exec_module(cleanup)


class Inventory(unittest.TestCase):
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

if __name__ == '__main__':unittest.main()
