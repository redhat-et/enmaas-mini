#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
import json
import contextlib
import io
import sys
import subprocess
import tempfile
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('rds_plan',(Path(__file__).resolve().parents[1] / 'rds.py'))
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
            'DBSubnetGroup':{'DBSubnetGroupArn':'arn:subnet','VpcId':plan['VpcId'],'DBSubnetGroupName':plan['Instance']['DBSubnetGroupName'],
                'SubnetGroupStatus':'Complete',
                'Subnets':[{'SubnetIdentifier':value,'SubnetStatus':'Active'} for value in plan['SubnetGroup']['SubnetIds']]},
            'DBParameterGroups':[{'DBParameterGroupName':plan['Instance']['DBParameterGroupName'],'ParameterApplyStatus':'in-sync'}],
            'MasterUserSecret':{'SecretArn':'arn:secret','SecretStatus':'active'},'Endpoint':{'Address':'db.example.test','Port':5432}}
        group={'OwnerId':plan['AccountId'],'VpcId':plan['VpcId'],'IpPermissions':[plan['Ingress']],'Tags':plan['Instance']['Tags']}
        def check(database,security_group,tags=None,parameter_arn='arn:parameter',tls=None):
            aws=Mock()
            aws.call.side_effect=[{'Account':plan['AccountId']},{'DBInstances':[database]},
                {'SecurityGroups':[security_group]},{'Parameters':plan['Parameters'] if tls is None else tls},
                {'DBParameterGroups':[{'DBParameterGroupArn':parameter_arn}]},
                *[{'TagList':(plan['Instance']['Tags'] if tags is None else
                             tags.get(index,plan['Instance']['Tags']) if isinstance(tags,dict) else tags)}
                  for index in range(3)]]
            return rds.verify(aws,state,plan['AccountId'],plan['Region'])
        self.assertTrue(check(db,group)['Ready'])
        for subnets in ([], db['DBSubnetGroup']['Subnets'][:1],
                        db['DBSubnetGroup']['Subnets'] * 2,
                        [{'SubnetIdentifier':'subnet-ffffffff','SubnetStatus':'Active'},
                         db['DBSubnetGroup']['Subnets'][1]]):
            changed={**db,'DBSubnetGroup':{**db['DBSubnetGroup'],'Subnets':subnets}}
            with self.subTest(subnets=subnets),self.assertRaisesRegex(ValueError,'subnet membership'):
                check(changed,group)
        reversed_group={**db['DBSubnetGroup'],'Subnets':list(reversed(db['DBSubnetGroup']['Subnets']))}
        self.assertTrue(check({**db,'DBSubnetGroup':reversed_group},group)['Ready'])
        for key,value in (('SubnetGroupStatus','Pending'),
                          ('Subnets',[{**row,'SubnetStatus':'Inactive'} for row in db['DBSubnetGroup']['Subnets']])):
            self.assertFalse(check({**db,'DBSubnetGroup':{**db['DBSubnetGroup'],key:value}},group)['Ready'])
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
        for field,value,message in (
                ('DBInstanceArn','arn:foreign','resource identity'),
                ('StorageType','standard','storage bounds'),
                ('AllocatedStorage',41,'storage bounds'),
                ('Endpoint',{'Address':'db.example.test','Port':5433},'port differs'),
                ('VpcSecurityGroups',[{'VpcSecurityGroupId':'sg-foreign'}],'network differs'),
                ('DBParameterGroups',[],'parameter group differs')):
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,message):
                check({**db,field:value},group)
        for field,value,message in (('OwnerId','999999999999','ownership differs'),
                                    ('Tags',[],'ownership tag differs')):
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,message):
                check(db,{**group,field:value})
        with self.assertRaisesRegex(ValueError,'TLS enforcement'):check(db,group,tls=[])
        for status in ('creating','rebooting'):
            self.assertFalse(check({**db,'DBInstanceStatus':status},group)['Ready'])
        self.assertFalse(check({**db,'DBParameterGroups':[{**db['DBParameterGroups'][0],
            'ParameterApplyStatus':'pending-reboot'}]},group)['Ready'])
        self.assertFalse(check({**db,'MasterUserSecret':{**db['MasterUserSecret'],
            'SecretStatus':'creating'}},group)['Ready'])
        without_endpoint=dict(db);del without_endpoint['Endpoint']
        self.assertIsNone(check(without_endpoint,group)['Endpoint'])
        self.assertFalse(check(without_endpoint,group)['Ready'])
        self.assertFalse(check({**db,'Endpoint':{'Port':5432,'Address':''}},group)['Ready'])
        for change in ('account','region','incomplete','pending','missing'):
            bad=copy.deepcopy(state)
            if change=='account':bad['Plan']['AccountId']='999999999999'
            if change=='region':bad['Plan']['Region']='us-east-1'
            if change=='incomplete':bad['Created']=False
            if change=='pending':bad['Pending']={'Name':'Instance'}
            aws=Mock();aws.call.side_effect=[{'Account':plan['AccountId']},{'DBInstances':[]}]
            with self.subTest(change=change),self.assertRaises(ValueError):
                rds.verify(aws,bad,plan['AccountId'],plan['Region'])
        broad=copy.deepcopy(group);broad['IpPermissions'][0]['IpRanges']=[{'CidrIp':'0.0.0.0/0'}]
        with self.assertRaises(ValueError):check(db,broad)

    def test_aws_client_rejects_partial_or_nonobject_responses(self):
        aws=rds.ReadOnlyAws('eu-central-1','scoped')
        for response in ({'DBInstances':[],'Marker':'unconsumed'},
                         {'Subnets':[],'NextToken':'unconsumed'},
                         {'DBInstances':[],'nextToken':'unconsumed'},[],None):
            with self.subTest(response=response),patch.object(rds.subprocess,'run',
                    return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response))):
                with self.assertRaisesRegex(ValueError,'incomplete or invalid'):
                    aws.call('rds','describe-db-instances')
        with patch.object(rds.subprocess,'run',return_value=SimpleNamespace(
                returncode=0,stdout='{"DBInstances":[],"Marker":""}')):
            self.assertEqual(aws.call('rds','describe-db-instances')['DBInstances'],[])

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
            spec=importlib.util.spec_from_file_location('network_for_rds_test',(Path(__file__).resolve().parents[1] / 'network.py'))
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
            for action,response,message in (
                    ('describe-security-groups',{'SecurityGroups':[]},'ownership'),
                    ('describe-subnets',{'Subnets':[]},'Subnet lookup'),
                    ('describe-vpc-attribute',{'EnableDnsSupport':{'Value':False},'EnableDnsHostnames':{'Value':False}},'DNS'),
                    ('describe-db-engine-versions',{'DBEngineVersions':[]},'version/family'),
                    ('describe-orderable-db-instance-options',{'OrderableDBInstanceOptions':[]},'not orderable')):
                def changed(service,operation,**options):
                    return response if operation==action else call(service,operation,**options)
                aws.call.side_effect=changed
                with self.subTest(action=action),self.assertRaisesRegex(ValueError,message):
                    rds.live_plan(aws,args)
            aws.call.side_effect=call
            original=journal.read_text()
            value=json.loads(original);value['Prefix']='another-gateway'
            journal.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'another lab run'):rds.live_plan(aws,args)
            journal.write_text(original)
            args.account_id='999999999999'
            with self.assertRaises(ValueError):rds.live_plan(aws,args)

    def test_wrong_subnet_vpc_zone_capacity_rejected(self):
        for field,value in [('VpcId','vpc-abcdef12'),('AvailabilityZone','eu-central-1a'),('AvailableIpAddressCount',0)]:
            data=self.inputs();data['subnets'][1][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):rds.build_plan(**data)

    def test_unbounded_or_ambiguous_settings_rejected(self):
        for field,value in [('account','wrong'),('engine_version','latest'),('instance_class','db.r7g.16xlarge'),('prefix','a--b'),('region','wrong'),('vpc','vpc-bad'),('gateway_group','sg-bad')]:
            data=self.inputs();data[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):rds.build_plan(**data)

    def test_subnet_count_identity_and_format_are_bounded(self):
        for mode in ('single','duplicate','bad-id'):
            data=self.inputs()
            if mode=='single':data['subnets']=data['subnets'][:1]
            if mode=='duplicate':data['subnets'][1]['SubnetId']=data['subnets'][0]['SubnetId']
            if mode=='bad-id':data['subnets'][1]['SubnetId']='subnet-invalid'
            with self.subTest(mode=mode),self.assertRaises(ValueError):rds.build_plan(**data)

    def test_read_client_uses_profile_and_hides_aws_error_text(self):
        aws=rds.ReadOnlyAws('eu-central-1','scoped-role')
        with patch.object(rds.subprocess,'run') as run:
            run.return_value=SimpleNamespace(returncode=0,stdout='{}')
            aws.call('rds','describe-db-instances',db_instance_identifier='test-db')
            command=run.call_args.args[0]
            self.assertEqual(command[command.index('--profile')+1],'scoped-role')
            self.assertEqual(run.call_args.kwargs['env']['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
            self.assertNotIn('AWS_ACCESS_KEY_ID',run.call_args.kwargs['env'])
            self.assertEqual(json.loads(command[-1]),{'DBInstanceIdentifier':'test-db'})
            run.return_value=SimpleNamespace(returncode=1,stdout='private',stderr='private')
            with self.assertRaises(ValueError) as caught:aws.call('sts','get-caller-identity')
            self.assertNotIn('private',str(caught.exception))

    def test_cli_offline_plan_and_verify_never_provision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'input.json'
            path.write_text(json.dumps(self.inputs()))
            output=io.StringIO()
            with patch('sys.argv',['rds.py','--input',str(path)]),patch.object(rds,'ProvisioningAws') as writer,contextlib.redirect_stdout(output):
                rds.main()
                writer.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())['Instance']['PubliclyAccessible'])
            with patch('sys.argv',['rds.py','--verify',str(path),'--account-id','123456789012','--region','eu-central-1']),patch.object(rds,'ReadOnlyAws') as reader,patch.object(rds,'verify',return_value={'Ready':False}) as verify,patch.object(rds,'ProvisioningAws') as writer,contextlib.redirect_stdout(io.StringIO()):
                rds.main()
                reader.assert_called_once_with('eu-central-1',None)
                verify.assert_called_once()
                writer.assert_not_called()

    def test_cli_requires_complete_and_unambiguous_operation(self):
        for flags in ([],['--apply'],['--verify','journal'],['--gateway-journal','gateway'],
                      ['--input','input','--gateway-journal','gateway']):
            with self.subTest(flags=flags),patch('sys.argv',['rds.py',*flags]),patch.object(rds,'ReadOnlyAws') as reader,patch.object(rds,'ProvisioningAws') as writer,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):rds.main()
                reader.assert_not_called()
                writer.assert_not_called()

    def test_cli_apply_requires_confirmation_and_unchanged_live_plan(self):
        plan=rds.build_plan(**self.inputs())
        base=['rds.py','--apply','--journal','new.json','--gateway-journal','gateway.json',
              '--account-id','123456789012','--region','eu-central-1','--prefix','lab-test',
              '--engine-version','17.6','--subnet-id','subnet-12345678','--subnet-id','subnet-abcdef12']
        confirmation='create RDS 123456789012 eu-central-1 lab-test'
        for answer,current,message in (('wrong',plan,'Confirmation'),(confirmation,{'changed':True},'Plan changed'),(confirmation,plan,None)):
            with self.subTest(message=message),patch('sys.argv',base),patch.object(rds,'ReadOnlyAws'),patch.object(rds,'live_plan',side_effect=[plan,current]),patch.object(rds,'ProvisioningAws') as writer,patch.object(rds,'create') as create,patch('builtins.input',return_value=answer),contextlib.redirect_stdout(io.StringIO()):
                if message:
                    with self.assertRaisesRegex(ValueError,message):rds.main()
                    create.assert_not_called()
                    writer.assert_not_called()
                else:
                    rds.main()
                    create.assert_called_once_with(writer.return_value,plan,Path('new.json'))

    def test_cli_failure_reports_no_automatic_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,str((Path(__file__).resolve().parents[1] / 'rds.py')),
                '--input',str(Path(tmp)/'missing')],text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Nothing was automatically deleted',result.stderr)

if __name__=='__main__':unittest.main()
