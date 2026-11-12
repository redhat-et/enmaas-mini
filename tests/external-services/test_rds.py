#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
import json
import tempfile
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('rds_plan',Path(__file__).with_name('rds.py'))
rds=importlib.util.module_from_spec(spec);spec.loader.exec_module(rds)

class RdsPlan(unittest.TestCase):
    def test_group_tag_is_applied_to_all_planned_database_resources(self):
        args = self.inputs()
        args['prefix'] = 'em-demo-0123456789ab'
        plan = rds.build_plan(**args)
        for key in ('Instance', 'SubnetGroup', 'ParameterGroup'):
            tags = {row['Key']: row['Value'] for row in plan[key]['Tags']}
            self.assertEqual(tags['enmaas:group'], 'demo')
            self.assertEqual(tags['enmaas:run'], args['prefix'])

    def inputs(self):
        return dict(account='123456789012',region='eu-central-1',prefix='lab-test',vpc='vpc-12345678',
            gateway_group='sg-12345678',engine_version='17.6',instance_class='db.t4g.small',subnets=[
            {'SubnetId':'subnet-'+id,'VpcId':'vpc-12345678','AvailabilityZone':az,'State':'available','AvailableIpAddressCount':10}
            for id,az in [('12345678','eu-central-1a'),('abcdef12','eu-central-1b')]])

    def test_private_encrypted_gateway_only_and_managed_credentials(self):
        plan=rds.build_plan(**self.inputs());instance=plan['Instance']
        self.assertFalse(instance['PubliclyAccessible']);self.assertTrue(instance['StorageEncrypted'])
        self.assertTrue(instance['DeletionProtection']);self.assertTrue(instance['ManageMasterUserPassword'])
        self.assertNotIn('MasterUserPassword',instance)
        self.assertNotIn('IpRanges',plan['Ingress']);self.assertEqual(plan['Ingress']['FromPort'],5432)
        self.assertEqual(plan['Parameters'][0]['ParameterValue'],'1')

    def test_creation_journals_each_partial_failure_without_deleting(self):
        plan=rds.build_plan(**self.inputs())
        responses=[{'GroupId':'sg-abcdef12'},{}, {'DBSubnetGroup':{'DBSubnetGroupArn':'arn:subnet'}},
            {'DBParameterGroup':{'DBParameterGroupArn':'arn:parameter'}},{},
            {'DBInstance':{'DBInstanceIdentifier':'lab-test-postgres','DBInstanceArn':'arn:db','DbiResourceId':'db-ORIGINAL','MasterUserSecret':{'SecretArn':'arn:secret'}}}]
        for failure in range(7):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                journal=Path(directory)/'rds.json';aws=Mock()
                aws.call.side_effect=responses[:failure]+([RuntimeError('simulated failure')] if failure<6 else [])
                if failure<6:
                    with self.assertRaises(RuntimeError):rds.create(aws,plan,journal)
                else:rds.create(aws,plan,journal)
                state=json.loads(journal.read_text())
                self.assertEqual(len(state['Resources']),failure)
                self.assertEqual(state['Created'],failure==6)
                self.assertEqual(state['Pending'] is None,failure==6)
                self.assertEqual(journal.stat().st_mode&0o777,0o600)
                self.assertTrue(all('delete' not in call.args[1] for call in aws.call.call_args_list))
                self.assertNotIn('MasterUserPassword',state['Plan']['Instance'])
                if failure==6:
                    payload=aws.call.call_args.kwargs
                    self.assertEqual(payload['VpcSecurityGroupIds'],['sg-abcdef12'])
                    self.assertTrue(payload['ManageMasterUserPassword'])
                    self.assertEqual(state['Resources']['Instance']['ResourceId'],'db-ORIGINAL')
                before=aws.call.call_count
                with self.assertRaises(FileExistsError):rds.create(aws,plan,journal)
                self.assertEqual(before,aws.call.call_count)

    def test_verifier_rejects_public_unencrypted_or_broad_ingress(self):
        import copy
        plan=rds.build_plan(**self.inputs())
        state={'Plan':plan,'Created':True,'Pending':None,'Resources':{'SecurityGroup':'sg-abcdef12', 'SubnetGroup':'arn:subnet', 'ParameterGroup':'arn:parameter', 'Instance':{'Arn':'arn:db', 'ResourceId':'db-ORIGINAL', 'MasterSecretArn':'arn:secret'}}}
        db={**plan['Instance'],'DBInstanceArn':'arn:db','DbiResourceId':'db-ORIGINAL','DBInstanceStatus':'available',
            'VpcSecurityGroups':[{'VpcSecurityGroupId':'sg-abcdef12'}],
            'DBSubnetGroup':{'DBSubnetGroupArn':'arn:subnet','VpcId':plan['VpcId'],'DBSubnetGroupName':plan['Instance']['DBSubnetGroupName']},
            'DBParameterGroups':[{'DBParameterGroupName':plan['Instance']['DBParameterGroupName'],'ParameterApplyStatus':'in-sync'}],
            'MasterUserSecret':{'SecretArn':'arn:secret','SecretStatus':'active'},'Endpoint':{'Address':'db.example.test','Port':5432}}
        group={'OwnerId':plan['AccountId'],'VpcId':plan['VpcId'],'IpPermissions':[plan['Ingress']],'Tags':plan['Instance']['Tags']}
        def check(database,security_group,tags=None,parameter_arn='arn:parameter'):
            aws=Mock()
            aws.call.side_effect=[{'Account':plan['AccountId']},{'DBInstances':[database]},
                {'SecurityGroups':[security_group]},{'Parameters':plan['Parameters']},
                {'DBParameterGroups':[{'DBParameterGroupArn':parameter_arn}]},
                *[{'TagList':(plan['Instance']['Tags'] if tags is None else
                             tags.get(index,plan['Instance']['Tags']) if isinstance(tags,dict) else tags)}
                  for index in range(3)]]
            return rds.verify(aws,state,plan['AccountId'],plan['Region'])
        self.assertTrue(check(db,group)['Ready'])
        original_id=state['Resources']['Instance'].pop('ResourceId')
        with self.assertRaisesRegex(ValueError,'immutable resource ID'):check(db,group)
        state['Resources']['Instance']['ResourceId']=original_id
        for changed in ({**db, 'DbiResourceId':'db-REPLACEMENT'},
                        {**db, 'MasterUserSecret': {'SecretArn': 'foreign', 'SecretStatus': 'active'}},
                        {**db, 'DBSubnetGroup': {**db['DBSubnetGroup'], 'DBSubnetGroupArn': 'foreign'}}):
            with self.assertRaises(ValueError):check(changed,group)
        for index in range(3):
            with self.subTest(dependency=index),self.assertRaises(ValueError):
                check(db,group,tags={index:[]})
        with self.assertRaises(ValueError):check(db,group,parameter_arn='foreign')

        for field,value in [('PubliclyAccessible',True),('StorageEncrypted',False),('DeletionProtection',False)]:
            changed={**db,field:value}
            with self.subTest(field=field),self.assertRaises(ValueError):check(changed,group)
        broad=copy.deepcopy(group);broad['IpPermissions'][0]['IpRanges']=[{'CidrIp':'0.0.0.0/0'}]
        with self.assertRaises(ValueError):check(db,broad)

    def test_cli_is_read_only_and_preserves_api_acronyms(self):
        aws=rds.ReadOnlyAws('eu-central-1')
        with patch.object(rds.subprocess,'run') as run:
            run.return_value=SimpleNamespace(returncode=0,stdout='{}')
            aws.call('rds','describe-orderable-db-instance-options',db_instance_class='db.t4g.small')
            payload=json.loads(run.call_args.args[0][-1])
            self.assertEqual(payload,{'DBInstanceClass':'db.t4g.small'})
            with self.assertRaises(ValueError):aws.call('rds','create-db-instance')
            self.assertEqual(run.call_count,1)

    def test_live_plan_verifies_account_and_orderable_encryption(self):
        data=self.inputs()
        with tempfile.TemporaryDirectory() as directory:
            journal=Path(directory)/'gateway.json'
            # Use the actual shared ownership-tag contract.
            spec=importlib.util.spec_from_file_location('network_for_rds_test',Path(__file__).with_name('network.py'))
            network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)
            tags=network.vm.tags('lab-test-gateway','remote-gateway')
            journal.write_text(json.dumps({'AccountId':data['account'],'Region':data['region'],
                'Prefix':'lab-test-gateway','Scenario':'remote-gateway','InstanceId':'i-test',
                'SecurityGroupId':data['gateway_group'],'Ingress':[]}))
            args=SimpleNamespace(account_id=data['account'],region=data['region'],prefix=data['prefix'],
                gateway_journal=journal,subnet_ids=[s['SubnetId'] for s in data['subnets']],
                engine_version=data['engine_version'],instance_class=data['instance_class'])
            def call(service,action,**options):
                if action=='get-caller-identity':return {'Account':data['account']}
                if action=='describe-instances':return {'Reservations':[{'Instances':[{
                    'InstanceId':'i-test','State':{'Name':'running'},'PublicIpAddress':'192.0.2.10',
                    'VpcId':data['vpc'],'Tags':tags,'SecurityGroups':[{'GroupId':data['gateway_group']}]}]}]}
                if action=='describe-security-groups':return {'SecurityGroups':[{'OwnerId':data['account'],'VpcId':data['vpc'],'Tags':tags}]}
                if action=='describe-subnets':return {'Subnets':data['subnets']}
                if action=='describe-vpc-attribute':return {'EnableDnsSupport':{'Value':True},'EnableDnsHostnames':{'Value':True}}
                if action=='describe-db-engine-versions':return {'DBEngineVersions':[{'EngineVersion':'17.6','DBParameterGroupFamily':'postgres17'}]}
                if action=='describe-orderable-db-instance-options':return {'OrderableDBInstanceOptions':[{'StorageType':'gp3','SupportsStorageEncryption':True}]}
                self.fail('Unexpected operation '+action)
            aws=Mock();aws.call.side_effect=call
            self.assertEqual(rds.live_plan(aws,args)['GatewayInstanceId'],'i-test')
            args.account_id='999999999999'
            with self.assertRaises(ValueError):rds.live_plan(aws,args)

    def test_wrong_subnet_vpc_zone_capacity_rejected(self):
        for field,value in [('VpcId','vpc-abcdef12'),('AvailabilityZone','eu-central-1a'),('AvailableIpAddressCount',0)]:
            data=self.inputs();data['subnets'][1][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):rds.build_plan(**data)

    def test_unbounded_or_ambiguous_settings_rejected(self):
        for field,value in [('account','wrong'),('engine_version','latest'),('instance_class','db.r7g.16xlarge'),('prefix','a--b')]:
            data=self.inputs();data[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):rds.build_plan(**data)

if __name__=='__main__':unittest.main()
