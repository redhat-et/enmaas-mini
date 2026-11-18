"""VM termination is explicit, journaled, scoped and never automatically retried."""
import copy
import contextlib
import io
import json
import subprocess
import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('terminate',(Path(__file__).resolve().parents[1] / 'cleanup-instances.py'))
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)

class Termination(unittest.TestCase):
    def manifest(self):return t.inventory.registry.new('123456789012','eu-central-1','/tmp/key','operator',group='lab')
    def snapshot(self):return {'instances':['i-12345678','i-23456789','i-34567890'],
        'root_volumes':['vol-12345678','vol-23456789','vol-34567890'],'vpc':'vpc-12345678'}
    def test_client_is_read_only_by_default_and_sanitizes_failures(self):
        m=self.manifest();client=t.Aws(m)
        with patch.object(t.subprocess,'run') as run,self.assertRaises(ValueError):
            client.call('ec2','terminate-instances',InstanceIds=['i-12345678'])
        run.assert_not_called()
        with patch.object(t.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{}')) as run:
            self.assertEqual(client.call('sts','get-caller-identity'),{})
        self.assertEqual(run.call_args.kwargs['timeout'],60)
        for response in (SimpleNamespace(returncode=1,stdout='SECRET'),SimpleNamespace(returncode=0,stdout='[]'),
                         SimpleNamespace(returncode=0,stdout='not json')):
            with patch.object(t.subprocess,'run',return_value=response),self.assertRaises(ValueError) as error:
                client.call('sts','get-caller-identity')
            self.assertNotIn('SECRET',str(error.exception))

    def test_live_snapshot_requires_primary_interface_and_stable_state(self):
        m=self.manifest();resources={'instances':{},'groups':{'sg-one':{}},'volumes':{},'keys':{}}
        dependencies=[];journals={}
        for index in range(3):
            iid='i-'+str(index+1)*8;eni='eni-'+str(index+1)*8
            resources['instances'][iid]={'State':{'Name':'running'},'VpcId':'vpc-12345678','SubnetId':'subnet-12345678',
                'NetworkInterfaces':[{'NetworkInterfaceId':eni,'Attachment':{'DeviceIndex':0}}]}
            journals[str(index)]={'InstanceId':iid,'VpcId':'vpc-12345678','SubnetId':'subnet-12345678'}
            dependencies.append({'id':eni,'instance':iid,'relationship':'journaled VM','delete_with_instance':True})
        rows=[{'kind':'volumes','id':'vol-12345678'},{'kind':'instances','id':'i-11111111'}]
        journals['unmatched']={'InstanceId':'i-99999999'}
        def collect(reader,manifest):
            reader.call('ec2','describe-instances',instance_ids=['i-11111111'])
            return resources
        aws=Mock()
        with patch.object(t.inventory.vm,'check_identity'),patch.object(t.inventory,'collect_resources',side_effect=collect), \
             patch.object(t.inventory,'inventory',return_value=rows),patch.object(t.inventory,'network_dependencies',return_value=dependencies):
            result=t.verified_snapshot(aws,m,journals)
            self.assertEqual(len(result['instances']),3)
            for change in ('state','vpc','interfaces','primary','missing','foreign-peer','journal-vpc','journal-subnet','invalid-vpc','multiple-vpcs'):
                original=copy.deepcopy(resources);original_deps=copy.deepcopy(dependencies);original_journals=copy.deepcopy(journals)
                first=next(iter(resources['instances'].values()))
                if change=='journal-vpc':journals['0']['VpcId']='vpc-87654321'
                if change=='journal-subnet':journals['0']['SubnetId']='subnet-87654321'
                if change=='state':first['State']['Name']='pending'
                if change=='vpc':first['VpcId']='vpc-23456789'
                if change=='invalid-vpc':first['VpcId']='invalid'
                if change=='multiple-vpcs':
                    first['VpcId']='vpc-23456789';journals['0']['VpcId']='vpc-23456789'
                if change=='interfaces':first['NetworkInterfaces'].append(copy.deepcopy(first['NetworkInterfaces'][0]))
                if change=='primary':first['NetworkInterfaces'][0]['Attachment']['DeviceIndex']=1
                if change=='missing':dependencies.pop()
                if change=='foreign-peer':dependencies[0]['relationship']='unresolved'
                with self.subTest(change=change),self.assertRaises(ValueError):t.verified_snapshot(aws,m,journals)
                resources.clear();resources.update(original);dependencies[:]=original_deps;journals.clear();journals.update(original_journals)

    def test_complete_inventory_boundary_before_termination(self):
        """Exercise the real validation chain with only AWS responses substituted."""
        m=self.manifest();journals={}
        responses={('sts','get-caller-identity'):{'Account':m['account'],
            'Arn':'arn:aws:iam::'+m['account']+':user/operator'}}
        instances=[];groups=[];keys=[];volumes=[];interfaces=[]
        for index,(role,scenario) in enumerate(t.inventory.ROLES.items()):
            suffix=str(index+1)*8
            iid='i-'+suffix;gid='sg-'+suffix;vid='vol-'+suffix;eni='eni-'+suffix
            prefix=m['id']+'-'+role;tags=t.inventory.vm.tags(prefix,scenario)
            journals[role]={'AccountId':m['account'],'Region':m['region'],'Prefix':prefix,
                'Scenario':scenario,'InstanceId':iid,'SecurityGroupId':gid,'KeyPairName':prefix,
                'RootDeviceName':'/dev/sda1','VpcId':'vpc-12345678','SubnetId':'subnet-'+suffix}
            instances.append({'InstanceId':iid,'Tags':tags,'KeyName':prefix,
                'VpcId':'vpc-12345678','SubnetId':'subnet-'+suffix,'State':{'Name':'running'},
                'SecurityGroups':[{'GroupId':gid}],
                'NetworkInterfaces':[{'NetworkInterfaceId':eni,'Attachment':{'DeviceIndex':0}}],
                'BlockDeviceMappings':[{'DeviceName':'/dev/sda1',
                    'Ebs':{'VolumeId':vid,'DeleteOnTermination':True}}]})
            groups.append({'GroupId':gid,'OwnerId':m['account'],'Tags':tags})
            keys.append({'KeyName':prefix,'Tags':tags})
            volumes.append({'VolumeId':vid,'Tags':tags,'Attachments':[{'InstanceId':iid}]})
            interfaces.append({'NetworkInterfaceId':eni,'OwnerId':m['account'],
                'Groups':[{'GroupId':gid}],'RequesterManaged':False,
                'Attachment':{'InstanceId':iid,'DeleteOnTermination':True}})
        responses.update({('ec2','describe-instances'):{'Reservations':[{'OwnerId':m['account'],'Instances':instances}]},
            ('ec2','describe-security-groups'):{'SecurityGroups':groups},
            ('ec2','describe-key-pairs'):{'KeyPairs':keys},
            ('ec2','describe-volumes'):{'Volumes':volumes},
            ('ec2','describe-network-interfaces'):{'NetworkInterfaces':interfaces}})
        for mode in ('valid','foreign-tag','retained-disk','foreign-interface','partial'):
            replies=copy.deepcopy(responses);aws=Mock()
            if mode=='foreign-tag':replies[('ec2','describe-volumes')]['Volumes'][0]['Tags']=[]
            if mode=='retained-disk':replies[('ec2','describe-instances')]['Reservations'][0]['Instances'][0]['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination']=False
            if mode=='foreign-interface':replies[('ec2','describe-network-interfaces')]['NetworkInterfaces'][0]['RequesterManaged']=True
            if mode=='partial':replies[('ec2','describe-instances')]['NextToken']='more'
            def call(service,action,**options):
                self.assertNotEqual(action,'terminate-instances')
                if service=='ec2':self.assertIn('Filters',options)
                return replies[(service,action)]
            aws.call.side_effect=call
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                journal=Path(tmp)/'termination'
                if mode=='valid':
                    result=t.execute(aws,m,journals,journal,apply=False,confirmation=None)
                    self.assertEqual(len(result['instances']),3)
                    self.assertEqual(len(result['root_volumes']),3)
                else:
                    with self.assertRaises(ValueError):
                        t.execute(aws,m,journals,journal,apply=True,confirmation=t.required_confirmation(m))
                self.assertFalse(journal.exists())

    def test_cleanup_policy_pins_exact_instances_and_all_ownership_tags(self):
        spec=importlib.util.spec_from_file_location('cleanup_eval',Path(__file__).with_name('test_network_policy.py'))
        evaluation=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluation)
        m=self.manifest();journals={}
        for index,(role,scenario) in enumerate(t.inventory.ROLES.items()):
            journals[role]={'AccountId':m['account'],'Region':m['region'],
                'Prefix':m['id']+'-'+role,'Scenario':scenario,'InstanceId':'i-'+str(index+1)*8}
        document=t.cleanup_policy(m,journals)
        for role,state in journals.items():
            arn='arn:aws:ec2:'+m['region']+':'+m['account']+':instance/'+state['InstanceId']
            context={'aws:RequestedRegion':m['region'],**{'aws:ResourceTag/'+x['Key']:x['Value']
                for x in t.inventory.vm.tags(state['Prefix'],state['Scenario'])}}
            self.assertTrue(evaluation.allowed(document,'ec2:TerminateInstances',arn,context))
            for field in context:
                wrong=dict(context);wrong[field]='other'
                self.assertFalse(evaluation.allowed(document,'ec2:TerminateInstances',arn,wrong))
            self.assertFalse(evaluation.allowed(document,'ec2:TerminateInstances',arn+'9',context))
            for action in ('ec2:DeleteVolume','ec2:DeleteTags','ec2:CreateTags','iam:PassRole'):
                self.assertFalse(evaluation.allowed(document,action,arn,context))
        for change in ('account','region','prefix','scenario','id','duplicate'):
            altered=copy.deepcopy(journals);first=altered['gateway']
            field={'account':'AccountId','region':'Region','prefix':'Prefix','scenario':'Scenario','id':'InstanceId'}.get(change)
            if field:first[field]='wrong'
            else:first['InstanceId']=altered['keycloak']['InstanceId']
            with self.subTest(change=change),self.assertRaises(ValueError):t.cleanup_policy(m,altered)

    def test_cli_preview_reads_only_recorded_journals(self):
        manifest=self.manifest()
        with patch.object(sys,'argv',['cleanup','--manifest','/tmp/manifest','--state-dir','/tmp/state','--journal','/tmp/ledger']), \
             patch.object(t.inventory.registry,'load',return_value=manifest), \
             patch.object(t.inventory.registry,'read_private_json',return_value={}) as read, \
             patch.object(t,'execute',return_value={'termination_requested':False}) as execute, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            t.main()
        self.assertEqual(json.loads(output.getvalue()),{'termination_requested':False})
        self.assertFalse(execute.call_args.kwargs['apply'])
        self.assertEqual({call.args[0].name for call in read.call_args_list},
                         {manifest['id']+'-'+role+'.json' for role in t.inventory.ROLES})

    def test_policy_cli_is_offline_and_requires_journal_for_preview(self):
        m=self.manifest()
        for policy in (True,False):
            args=['cleanup','--manifest','/tmp/manifest','--state-dir','/tmp/state']
            if policy:args.append('--policy')
            with patch.object(sys,'argv',args),patch.object(t.inventory.registry,'load',return_value=m), \
                 patch.object(t.inventory.registry,'read_private_json',return_value={}), \
                 patch.object(t,'cleanup_policy',return_value={'Version':'2012-10-17'}) as render, \
                 patch.object(t,'Aws') as aws,contextlib.redirect_stdout(io.StringIO()) as output:
                if policy:
                    t.main();self.assertEqual(json.loads(output.getvalue()),{'Version':'2012-10-17'})
                    render.assert_called_once()
                else:
                    with self.assertRaisesRegex(ValueError,'journal'):t.main()
                    render.assert_not_called()
                aws.assert_not_called()
        result=subprocess.run([sys.executable,str(Path(t.__file__)),'--manifest','unused',
            '--state-dir','unused','--policy','--apply'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('not allowed with argument',result.stderr)

    def test_cli_rejects_missing_manifest_without_exposing_paths(self):
        result=subprocess.run([sys.executable,str(Path(t.__file__)),
            '--manifest','/nonexistent/PRIVATE-manifest','--state-dir','/nonexistent',
            '--journal','/nonexistent/ledger'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Cleanup stopped',result.stderr)
        self.assertNotIn('PRIVATE',result.stderr)
        self.assertEqual(result.stdout,'')

    def test_preview_does_not_create_journal_or_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=self.manifest();aws=Mock();path=Path(tmp)/'journal'
            with patch.object(t,'verified_snapshot',return_value=self.snapshot()):
                r=t.execute(aws,m,{},path,apply=False,confirmation=None)
            self.assertFalse(r['termination_requested']);self.assertFalse(path.exists());aws.call.assert_not_called()
    def test_exact_confirmation_before_any_aws_access(self):
        with patch.object(t,'verified_snapshot') as snapshot,self.assertRaises(ValueError):
            t.execute(Mock(),self.manifest(),{},Path('/unused'),apply=True,confirmation='wrong')
        snapshot.assert_not_called()
    def test_drift_refuses_and_keeps_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=self.manifest();aws=Mock();changed={**self.snapshot(),'vpc':'vpc-other'}
            with patch.object(t,'verified_snapshot',side_effect=[self.snapshot(),changed]),self.assertRaises(ValueError):
                t.execute(aws,m,{},Path(tmp)/'journal',apply=True,confirmation=t.required_confirmation(m))
            aws.call.assert_not_called()
            self.assertTrue((Path(tmp)/'journal'/'0000.json').exists())
    def test_success_journals_intent_before_one_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=self.manifest();path=Path(tmp)/'journal';aws=Mock()
            def call(service,action,**options):
                self.assertTrue((path/'0001.json').exists())
                self.assertEqual((service,action),('ec2','terminate-instances'))
                self.assertEqual(options,{'InstanceIds':self.snapshot()['instances']})
                return {'TerminatingInstances':[{'InstanceId':i,'CurrentState':{'Name':'shutting-down'}} for i in options['InstanceIds']]}
            aws.call.side_effect=call
            with patch.object(t,'verified_snapshot',return_value=self.snapshot()):
                r=t.execute(aws,m,{},path,apply=True,confirmation=t.required_confirmation(m))
            self.assertTrue(r['termination_requested']);self.assertFalse(r['termination_verified'])
            self.assertEqual(aws.call.call_count,1)
            with patch.object(t,'verified_snapshot',return_value=self.snapshot()),self.assertRaises(FileExistsError):
                t.execute(aws,m,{},path,apply=True,confirmation=t.required_confirmation(m))
            self.assertEqual(aws.call.call_count,1)
    def test_uncertain_response_never_retries(self):
        for response in (OSError('SECRET'),{}, {'TerminatingInstances':[{'InstanceId':'i-foreign','CurrentState':{'Name':'terminated'}}]}):
            with tempfile.TemporaryDirectory() as tmp:
                m=self.manifest();aws=Mock();path=Path(tmp)/'journal'
                if isinstance(response,Exception):aws.call.side_effect=response
                else:aws.call.return_value=response
                with patch.object(t,'verified_snapshot',return_value=self.snapshot()),self.assertRaisesRegex(ValueError,'uncertain') as error:
                    t.execute(aws,m,{},path,apply=True,confirmation=t.required_confirmation(m))
                self.assertNotIn('SECRET',str(error.exception));self.assertEqual(aws.call.call_count,1)
                self.assertTrue((path/'0001.json').exists());self.assertFalse((path/'0002.json').exists())

    def test_read_only_verification_after_known_or_uncertain_termination(self):
        m=self.manifest();journals={};instances=[]
        for index,(role,scenario) in enumerate(t.inventory.ROLES.items()):
            state={'AccountId':m['account'],'Region':m['region'],'Prefix':m['id']+'-'+role,
                   'Scenario':scenario,'InstanceId':'i-'+str(index+1)*8}
            journals[role]=state
            instances.append({'InstanceId':state['InstanceId'],'State':{'Name':'terminated'},
                'Tags':t.inventory.vm.tags(state['Prefix'],scenario)})
        ids=sorted(row['InstanceId'] for row in journals.values())
        plan={'deployment':m['id'],'account':m['account'],'region':m['region'],'instances':ids,
              'root_volumes':self.snapshot()['root_volumes']}
        events=[{'event':'plan','result':plan},
                {'event':'intent','action':'terminate-instances','instances':ids},
                {'event':'request-recorded','action':'terminate-instances','instances':ids}]
        response={'Reservations':[{'OwnerId':m['account'],'Instances':instances}]}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'termination';root.mkdir(mode=0o700)
            def save(records):
                for path in root.iterdir():path.unlink()
                for index,record in enumerate(records):
                    path=root/(str(index).zfill(4)+'.json');path.touch(mode=0o600);path.write_text(json.dumps(record))
            def check(instance_response=None,disks=None):
                aws=Mock();aws.call.side_effect=[{'Account':m['account'],'Arn':'arn:aws:iam::'+m['account']+':user/operator'},
                    response if instance_response is None else instance_response,
                    {'Volumes':[]} if disks is None else disks]
                value=t.verify_termination(aws,m,journals,root)
                self.assertEqual([call.args[1] for call in aws.call.call_args_list],
                    ['get-caller-identity','describe-instances','describe-volumes'])
                return value
            for records in (events[:2],events):
                save(records);self.assertTrue(check()['termination_verified'])
            self.assertFalse(check({'Reservations':[]})['termination_verified'])
            changed=copy.deepcopy(response);changed['Reservations'][0]['Instances'][0]['State']['Name']='shutting-down'
            self.assertFalse(check(changed)['termination_verified'])
            self.assertFalse(check(disks={'Volumes':[{'VolumeId':plan['root_volumes'][0]}]})['termination_verified'])
            for kind in ('page','owner','duplicate','foreign','tags'):
                changed=copy.deepcopy(response)
                if kind=='page':changed['NextToken']='more'
                if kind=='owner':changed['Reservations'][0]['OwnerId']='999999999999'
                if kind=='duplicate':changed['Reservations'][0]['Instances'].append(copy.deepcopy(instances[0]))
                if kind=='foreign':changed['Reservations'][0]['Instances'][0]['InstanceId']='i-ffffffff'
                if kind=='tags':changed['Reservations'][0]['Instances'][0]['Tags']=[]
                with self.subTest(kind=kind),self.assertRaises(ValueError):check(changed)
            for disks in ({'Volumes':[],'NextToken':'more'},
                          {'Volumes':[{'VolumeId':plan['root_volumes'][0]}]*2},
                          {'Volumes':[{'VolumeId':'vol-ffffffff'}]}):
                with self.assertRaises(ValueError):check(disks=disks)
            for kind in ('count','plan','account','intent','result','volumes','duplicate-volume','invalid-volume'):
                changed=copy.deepcopy(events)
                if kind=='count':changed=events[:1]
                if kind=='plan':changed[0]['event']='wrong'
                if kind=='account':changed[0]['result']['account']='999999999999'
                if kind=='intent':changed[1]['instances']=[]
                if kind=='result':changed[2]['event']='wrong'
                if kind=='volumes':changed[0]['result']['root_volumes']=[]
                if kind=='duplicate-volume':changed[0]['result']['root_volumes']=[plan['root_volumes'][0]]*3
                if kind=='invalid-volume':changed[0]['result']['root_volumes'][0]='invalid'
                save(changed)
                with self.subTest(kind=kind),self.assertRaises(ValueError):check()
            save(events);root.chmod(0o755)
            with self.assertRaises(ValueError):check()
            root.chmod(0o700)
            with patch.object(sys,'argv',['cleanup','--manifest','manifest','--state-dir','state','--journal',str(root),'--verify']), \
                 patch.object(t.inventory.registry,'load',return_value=m), \
                 patch.object(t.inventory.registry,'read_private_json',return_value={}), \
                 patch.object(t,'verify_termination',return_value={'writes_performed':False}) as verify, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                t.main()
                self.assertFalse(json.loads(output.getvalue())['writes_performed'])
                self.assertFalse(verify.call_args.args[0].apply)

if __name__=='__main__':unittest.main()
