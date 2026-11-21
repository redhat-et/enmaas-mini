"""Post-termination group planning must remain read-only and reject ambiguity."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('cleanup_groups',(Path(__file__).resolve().parents[1] / 'cleanup-groups.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)

class Groups(unittest.TestCase):
    def fixture(self):
        m=g.t.inventory.registry.new('123456789012','eu-central-1','/tmp/key','operator',group='lab')
        journals={};groups=[]
        for i,(role,scenario) in enumerate(g.t.inventory.ROLES.items(),1):
            prefix=m['id']+'-'+role
            journals[role]={'AccountId':m['account'],'Region':m['region'],'Prefix':prefix,
                'Scenario':scenario,'InstanceId':'i-'+str(i)*8,'SecurityGroupId':'sg-'+str(i)*8,
                'VpcId':'vpc-'+'a'*8}
            groups.append({'GroupId':'sg-'+str(i)*8,'GroupName':prefix,'OwnerId':m['account'],
                'VpcId':'vpc-'+'a'*8,'Tags':g.t.inventory.vm.tags(prefix,scenario)})
        return m,journals,groups

    def inspect(self,m,j,groups,interfaces=(),references=(),terminated=True):
        aws=Mock();aws.call.return_value={'SecurityGroups':groups}
        with patch.object(g.t,'verify_termination',return_value={'termination_verified':terminated}), \
             patch.object(g.t.inventory,'network_dependencies',return_value=list(interfaces)), \
             patch.object(g.t.inventory,'group_references',return_value=list(references)):
            result=g.plan(aws,m,j,Path('/tmp/termination'))
        return result,aws

    def test_owned_internal_references_ordered_before_group_removal(self):
        m,j,groups=self.fixture()
        ref={'source_group':groups[0]['GroupId'],'target_group':groups[1]['GroupId'],
             'source_owned':True,'direction':'ingress','protocol':'tcp','from_port':8443,'to_port':8443}
        result,aws=self.inspect(m,j,groups,references=[ref])
        self.assertFalse(result['writes_performed']);self.assertFalse(result['deletion_authorized'])
        self.assertFalse(result['blockers'])
        self.assertEqual([step['stage'] for step in result['steps']],['remove-internal-references','remove-owned-groups'])
        self.assertEqual(result['steps'][0]['references'],[ref])
        self.assertEqual(len(result['steps'][1]['group_ids']),3)
        aws.call.assert_called_once_with('ec2','describe-security-groups',GroupIds=sorted(row['GroupId'] for row in groups))

    def test_dependencies_block_steps_instead_of_being_adopted(self):
        m,j,groups=self.fixture()
        ref={'source_group':'sg-ffffffff','target_group':groups[0]['GroupId'],'source_owned':False}
        for interfaces,references in (([{'id':'eni-aaaaaaaa'}],[]),([],[ref])):
            result,_=self.inspect(m,j,groups,interfaces,references)
            self.assertTrue(result['blockers']);self.assertEqual(result['steps'],[])
            self.assertFalse(result['deletion_authorized'])
        with self.assertRaises(ValueError):self.inspect(m,j,groups,terminated=False)

    def test_group_identity_and_account_boundaries(self):
        m,j,rows=self.fixture()
        for field,value in (('OwnerId','999999999999'),('VpcId','vpc-bbbbbbbb'),
                            ('GroupName','default'),('Tags',[])):
            groups=copy.deepcopy(rows);groups[0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.inspect(m,j,groups)
        for groups in (rows[:2],rows+[rows[0]],rows[:2]+[dict(rows[2],GroupId='sg-ffffffff')]):
            with self.assertRaises(ValueError):self.inspect(m,j,groups)
        for change in ({'SecurityGroupId':'bad'},{'VpcId':'bad'},
                       {'SecurityGroupId':j['gateway']['SecurityGroupId']},
                       {'VpcId':'vpc-bbbbbbbb'}):
            modified=copy.deepcopy(j);modified['keycloak'].update(change)
            with self.assertRaises(ValueError):g.recorded_groups(m,modified)
        with self.assertRaises(ValueError):g.recorded_groups(m,dict(j,extra=j['gateway']))

    def test_partial_results_and_write_operations_are_refused(self):
        m,j,rows=self.fixture();aws=Mock();aws.call.return_value={'SecurityGroups':rows,'NextToken':'more'}
        with patch.object(g.t,'verify_termination',return_value={'termination_verified':True}),self.assertRaises(ValueError):
            g.plan(aws,m,j,Path('/tmp/termination'))
        for action in ('terminate-instances','delete-security-group','revoke-security-group-ingress'):
            with self.assertRaises(ValueError):g.Aws(m,True).call('ec2',action)

    def test_real_dependency_readers_use_aws_json_filters(self):
        m,j,groups=self.fixture();calls=[]
        def call(service,action,**options):
            calls.append((action,options))
            if action=='describe-network-interfaces':return {'NetworkInterfaces':[]}
            if 'GroupIds' in options:return {'SecurityGroups':groups}
            return {'SecurityGroups':[]}
        aws=Mock();aws.call.side_effect=call
        with patch.object(g.t,'verify_termination',return_value={'termination_verified':True}):
            result=g.plan(aws,m,j,Path('/tmp/termination'))
        self.assertEqual(len(calls),4)
        self.assertFalse(result['blockers'])
        self.assertTrue(all('Filters' in options for _,options in calls[1:]))

    def test_cli_invalid_manifest_stops_without_cloud_calls(self):
        import subprocess,sys,tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,g.__file__,'--manifest',str(Path(tmp)/'missing'),
                '--state-dir',tmp,'--termination-journal',tmp],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(result.stdout,'')
        self.assertIn('No writes performed',result.stderr)

    def test_cli_loads_private_journals_and_uses_read_only_client(self):
        import contextlib,io
        m,j,_=self.fixture()
        with patch('sys.argv',['groups','--manifest','manifest','--state-dir','states','--termination-journal','termination']), \
             patch.object(g.t.inventory.registry,'load',return_value=m), \
             patch.object(g.t.inventory.registry,'read_private_json',side_effect=list(j.values())), \
             patch.object(g,'Aws') as aws,patch.object(g,'plan',return_value={'writes_performed':False}) as plan, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            g.main()
        aws.assert_called_once_with(m);self.assertIn('false',out.getvalue())
        self.assertEqual(plan.call_args.args[-1],Path('termination'))

if __name__=='__main__':unittest.main()
