"""Evaluate the generated policy's limited condition vocabulary offline, not AWS IAM."""
import fnmatch
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('network_policy',(Path(__file__).resolve().parents[1] / 'network-policy.py'))
policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)


def allowed(document,action,resource,context):
    for statement in document['Statement']:
        if action not in statement['Action'] or not any(fnmatch.fnmatchcase(resource,arn) for arn in statement['Resource']):continue
        matches=True
        for operator,conditions in statement.get('Condition',{}).items():
            for key,expected in conditions.items():
                actual=context.get(key)
                expected=expected if isinstance(expected,list) else [expected]
                if operator=='StringEquals':value=actual in expected
                elif operator=='StringLike':value=isinstance(actual,str) and any(fnmatch.fnmatchcase(actual,p) for p in expected)
                elif operator=='ForAllValues:StringEquals':value=actual is None or all(v in expected for v in actual)
                elif operator=='Null':value=(actual is None)==(expected[0]=='true')
                elif operator=='Bool':value=str(actual).lower()==expected[0]
                elif operator=='NumericEquals':value=actual is not None and float(actual)==float(expected[0])
                elif operator=='NumericLessThanEquals':value=actual is not None and float(actual)<=float(expected[0])
                else:raise AssertionError('Unreviewed test condition: '+operator)
                matches=matches and value
        if matches:return True
    return False


class NetworkPolicy(unittest.TestCase):
    def setUp(self):
        self.manifest=policy.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')
        self.document=policy.network_policy(self.manifest)
        self.arn='arn:aws:ec2:eu-central-1:123456789012:'

    def context(self,source='RequestTag'):
        return {'aws:RequestedRegion':'eu-central-1', 'aws:'+source+'/enmaas:group':'demo',
                'aws:'+source+'/enmaas:run':self.manifest['id'],
                'aws:'+source+'/enmaas:purpose':'external-services-test',
                'aws:TagKeys':['enmaas:group','enmaas:run','enmaas:purpose','Name']}

    def test_create_requires_group_run_purpose_region_and_known_tag_keys(self):
        for action,kind in (('CreateVpc','vpc'),('CreateSubnet','subnet'),('CreateRouteTable','route-table'),('CreateInternetGateway','internet-gateway')):
            resource=self.arn+kind+'/new'
            self.assertTrue(allowed(self.document,'ec2:'+action,resource,self.context()))
            for key in self.context():
                context=self.context();context.pop(key)
                self.assertFalse(allowed(self.document,'ec2:'+action,resource,context),key)
            for key,value in (('aws:RequestedRegion','us-east-1'),('aws:RequestTag/enmaas:group','other'),
                              ('aws:RequestTag/enmaas:run','em-other-0123456789ab'),
                              ('aws:RequestTag/enmaas:purpose','production'),('aws:TagKeys',['admin'])):
                context=self.context();context[key]=value
                self.assertFalse(allowed(self.document,'ec2:'+action,resource,context))
            self.assertFalse(allowed(self.document,'ec2:'+action,resource.replace('123456789012','999999999999'),self.context()))

    def test_existing_resource_checks_require_tags_not_request_claims(self):
        for action,kinds in policy.EXISTING.items():
            for kind in kinds:
                resource=self.arn+kind+'/existing'
                self.assertTrue(allowed(self.document,'ec2:'+action,resource,self.context('ResourceTag')))
                self.assertFalse(allowed(self.document,'ec2:'+action,resource,self.context()))
                for tag in ('enmaas:group','enmaas:run','enmaas:purpose'):
                    context=self.context('ResourceTag');context['aws:ResourceTag/'+tag]='foreign'
                    self.assertFalse(allowed(self.document,'ec2:'+action,resource,context))

    def test_standalone_retagging_deletion_and_escalation_not_granted(self):
        for kind,create_action in policy.NEW.items():
            resource=self.arn+kind+'/new';context=self.context()
            self.assertFalse(allowed(self.document,'ec2:CreateTags',resource,context))
            context['ec2:CreateAction']=create_action
            self.assertTrue(allowed(self.document,'ec2:CreateTags',resource,context))
            context['ec2:CreateAction']='RunInstances'
            self.assertFalse(allowed(self.document,'ec2:CreateTags',resource,context))
        for action in ('ec2:DeleteTags','ec2:DeleteVpc','ec2:RunInstances','iam:PassRole','iam:PutRolePolicy','secretsmanager:GetSecretValue'):
            self.assertFalse(allowed(self.document,action,self.arn+'vpc/existing',self.context('ResourceTag')))
        for statement in self.document['Statement']:
            self.assertTrue(all('*' not in action for action in statement['Action']))
            if statement['Resource']==['*']:
                self.assertTrue(all(action.startswith('ec2:Describe') or action=='sts:GetCallerIdentity' for action in statement['Action']))

    def test_discovery_is_regional_metadata_and_not_tag_scoped(self):
        self.assertTrue(allowed(self.document,'sts:GetCallerIdentity','*',{}))
        self.assertTrue(allowed(self.document,'ec2:DescribeVpcs','*',{'aws:RequestedRegion':'eu-central-1'}))
        self.assertFalse(allowed(self.document,'ec2:DescribeVpcs','*',{'aws:RequestedRegion':'us-east-1'}))

    def test_rejects_legacy_and_unqualified_partitions(self):
        legacy=policy.registry.new('123456789012','eu-central-1','/tmp/key','scoped')
        with self.assertRaises(ValueError):policy.network_policy(legacy)
        for region in ('cn-north-1','us-gov-west-1'):
            manifest=dict(self.manifest,region=region)
            with self.assertRaises(ValueError):policy.network_policy(manifest)

    def test_cli_emits_reviewable_document_and_rejects_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=policy.registry.save(Path(tmp)/'registry',self.manifest)
            command=[sys.executable,str((Path(__file__).resolve().parents[1] / 'network-policy.py')),str(path)]
            result=subprocess.run(command,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout),self.document)
            command[-1]=str(Path(tmp)/'missing')
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Network policy stopped:',result.stderr)

if __name__=='__main__':unittest.main()
