"""Operator bootstrap is preview-first, collision-refusing and journaled."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import io
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('operator_bootstrap',(Path(__file__).resolve().parents[1] / 'operator-bootstrap.py'))
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)


class Bootstrap(unittest.TestCase):
    def test_optional_dns_grants_only_selected_zone_and_group_namespace(self):
        import fnmatch
        policy=bootstrap.dns_policy(self.manifest,'ZEXAMPLE','lab.example.com')
        read,write=policy['Statement']
        self.assertEqual(read['Resource'],'arn:aws:route53:::hostedzone/ZEXAMPLE')
        self.assertEqual(write['Resource'],read['Resource'])
        self.assertEqual(write['Action'],'route53:ChangeResourceRecordSets')
        condition=write['Condition']
        patterns=condition['ForAllValues:StringLike']['route53:ChangeResourceRecordSetsNormalizedRecordNames']
        for role in ('gateway','keycloak','observability','_enmaas-owner'):
            self.assertTrue(any(fnmatch.fnmatchcase(role+'.em-demo-abcdef123456.lab.example.com',p) for p in patterns))
        for name in ('lab.example.com','gateway.em-other-abcdef123456.lab.example.com',
                     '_acme-challenge.em-demo-abcdef123456.lab.example.com',
                     'gateway.em-demo-abcdef123456.other.example.com'):
            self.assertFalse(any(fnmatch.fnmatchcase(name,p) for p in patterns))
        exact=condition['ForAllValues:StringEquals']
        self.assertEqual(exact['route53:ChangeResourceRecordSetsActions'],['CREATE','DELETE'])
        self.assertEqual(exact['route53:ChangeResourceRecordSetsRecordTypes'],['A','TXT'])
        self.assertEqual(len(condition['Null']),3)
        self.assertTrue(all(v=='false' for v in condition['Null'].values()))
        for zone,domain in (('',''),('','lab.example.com'),('ZEXAMPLE',''),('Z*','lab.example.com'),('ZEXAMPLE','*.example.com')):
            with self.assertRaises(ValueError):bootstrap.dns_policy(self.manifest,zone,domain)
        proposal=bootstrap.plan(self.manifest,'arn:aws:iam::123456789012:user/operator',
            ['ami-12345678'],['m7i.2xlarge'],150,dns_zone='ZEXAMPLE',dns_domain='lab.example.com')
        self.assertEqual(proposal['dns_scope'],{'zone_id':'ZEXAMPLE','domain':'lab.example.com'})
        self.assertIn('route53:ChangeResourceRecordSets',json.dumps(proposal['policies']))
        legacy=bootstrap.vm.network.registry.new('123456789012','eu-central-1','/tmp/key','test')
        with self.assertRaises(ValueError):bootstrap.dns_policy(legacy,'ZEXAMPLE','lab.example.com')
        for kwargs in ({'dns_zone':'ZEXAMPLE'},{'dns_domain':'lab.example.com'}):
            with self.assertRaises(ValueError):bootstrap.plan(self.manifest,'arn:aws:iam::123456789012:user/operator',
                ['ami-12345678'],['m7i.2xlarge'],150,**kwargs)

    def test_client_is_explicit_profile_no_retries_and_redacts_diagnostics(self):
        client=bootstrap.Aws('admin-profile','eu-central-1')
        with patch.dict(bootstrap.os.environ,{'AWS_ACCESS_KEY_ID':'unrelated-secret'}), \
                patch.object(bootstrap.subprocess,'run',return_value=Mock(returncode=0,stdout='{}')) as run:
            self.assertEqual(client.call('iam','get-account-summary'),{})
            self.assertIn('admin-profile',run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['AWS_MAX_ATTEMPTS'],'1')
            self.assertEqual(run.call_args.kwargs['env']['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
            self.assertNotIn('AWS_ACCESS_KEY_ID',run.call_args.kwargs['env'])
        with self.assertRaises(ValueError):bootstrap.Aws('--bad','eu-central-1')
        with self.assertRaises(ValueError):client.call('iam','create-access-key')
        for result in (Mock(returncode=1,stdout='',stderr='An error occurred (NoSuchEntity) secret'),
                       Mock(returncode=1,stdout='',stderr='secret arbitrary error'),
                       Mock(returncode=0,stdout='not json secret'),Mock(returncode=0,stdout='[]')):
            with patch.object(bootstrap.subprocess,'run',return_value=result),self.assertRaises(bootstrap.AwsFailure) as caught:
                client.call('iam','get-role',RoleName='example')
            self.assertNotIn('secret',str(caught.exception))
        for error in (OSError('secret'),subprocess.TimeoutExpired(['secret'],60)):
            with patch.object(bootstrap.subprocess,'run',side_effect=error),self.assertRaises(bootstrap.AwsFailure):
                client.call('iam','get-account-summary')
        with patch.object(bootstrap.subprocess,'run',return_value=Mock(returncode=0,stdout='')):
            self.assertEqual(client.call('iam','attach-role-policy'),{})

    def test_cli_preview_and_explicit_apply(self):
        args=['operator-bootstrap','--manifest','unused','--principal-arn','arn:aws:iam::123456789012:user/operator',
              '--image-id','ami-12345678','--instance-type','m7i.2xlarge','--max-volume-gib','150']
        with patch.object(sys,'argv',args),patch.object(bootstrap.vm.network.registry,'load',return_value=self.manifest), \
                patch.object(bootstrap,'Aws') as client,patch('sys.stdout',new_callable=io.StringIO) as output:
            bootstrap.main()
            self.assertFalse(json.loads(output.getvalue())['aws_changes_made'])
            client.assert_not_called()
        with patch.object(sys,'argv',args+['--dns-zone-id','ZEXAMPLE','--dns-domain','lab.example.com']),patch.object(bootstrap.vm.network.registry,'load',return_value=self.manifest), \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            bootstrap.main()
            self.assertEqual(json.loads(output.getvalue())['dns_scope']['zone_id'],'ZEXAMPLE')
        with patch.object(sys,'argv',args+['--apply']),patch.object(bootstrap.vm.network.registry,'load',return_value=self.manifest), \
                self.assertRaises(ValueError):bootstrap.main()
        with tempfile.TemporaryDirectory() as tmp:
            argv=args+['--apply','--admin-profile','admin-profile','--journal',str(Path(tmp)/'bootstrap'),
                       '--confirm','create operator demo 123456789012']
            with patch.object(sys,'argv',argv),patch.object(bootstrap.vm.network.registry,'load',return_value=self.manifest), \
                    patch.object(bootstrap,'Aws') as client,patch('sys.stdout',new_callable=io.StringIO) as output:
                client.return_value.call.side_effect=self.response
                bootstrap.main()
                self.assertEqual(json.loads(output.getvalue())['role_arn'],self.plan['role_arn'])
        result=subprocess.run([sys.executable,str((Path(__file__).resolve().parents[1] / 'operator-bootstrap.py'))]+args[1:],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Operator bootstrap stopped:',result.stderr)

    def test_group_preview_does_not_require_a_deployment_or_aws_credentials(self):
        args=['operator-bootstrap','--group','demo','--account','123456789012',
              '--principal-arn','arn:aws:iam::123456789012:user/operator',
              '--image-id','ami-12345678','--instance-type','m7i.2xlarge','--max-volume-gib','150']
        with patch.object(sys,'argv',args),patch.object(bootstrap,'Aws') as client, \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            bootstrap.main()
            result=json.loads(output.getvalue())
            self.assertEqual(result['region'],'eu-central-1')
            self.assertEqual(result['policies'],self.plan['policies'])
            client.assert_not_called()
        missing=args.copy();del missing[missing.index('--account'):missing.index('--account')+2]
        with patch.object(sys,'argv',missing),self.assertRaises(ValueError):bootstrap.main()

    def setUp(self):
        self.manifest=bootstrap.vm.network.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')
        self.plan=bootstrap.plan(self.manifest,'arn:aws:iam::123456789012:user/operator',
                                 ['ami-12345678'],['m7i.2xlarge'],150)

    def response(self,service,action,**options):
        if action=='get-caller-identity':return {'Account':'123456789012','Arn':'arn:aws:iam::123456789012:user/admin'}
        if action=='get-account-summary':return {'SummaryMap':{'Policies':5,'PoliciesQuota':1500,'Roles':5,'RolesQuota':1000,'AttachedPoliciesPerRoleQuota':10}}
        if action in ('get-role','get-policy'):raise bootstrap.AwsFailure('NoSuchEntity')
        if action=='validate-policy':return {'findings':[]}
        if action=='create-role':return {'Role':{'Arn':self.plan['role_arn'],'RoleId':'AROA12345678901234567'}}
        if action=='create-policy':return {'Policy':{'Arn':'arn:aws:iam::123456789012:policy/enmaas/'+options['PolicyName'],'PolicyId':'ANPA12345678901234567'}}
        return {}

    def apply(self,aws,path,confirmation=None):
        return bootstrap.apply(aws,self.plan,path,confirmation or 'create operator demo 123456789012')

    def test_plan_exact_principal_mfa_bounded_policies_and_no_admin_grants(self):
        self.assertEqual(self.plan['trust']['Statement'][0]['Condition'],{'Bool':{'aws:MultiFactorAuthPresent':'true'}})
        self.assertEqual(self.plan['role_name'],'em-demo-operator')
        self.assertLessEqual(len(self.plan['policies']),10)
        actions={a for d in self.plan['policies'] for s in d['Statement'] for a in s['Action']}
        self.assertNotIn('iam:PassRole',actions)
        self.assertFalse(any(a.startswith('iam:') for a in actions))
        self.assertFalse(self.plan['rds_supported'])
        with self.assertRaises(ValueError):bootstrap.plan(self.manifest,'arn:aws:iam::123456789012:root',['ami-12345678'],['m7i.2xlarge'],150)

    def test_success_journals_ownership_and_each_write(self):
        aws=Mock();aws.call.side_effect=self.response
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bootstrap';result=self.apply(aws,path)
            self.assertEqual(result['role_arn'],self.plan['role_arn'])
            self.assertFalse(result['live_permissions_qualified'])
            events=[json.loads(p.read_text()) for p in sorted(path.glob('*.json'))]
            self.assertEqual(events[-1]['event'],'complete')
            self.assertEqual(len([e for e in events if e['event']=='intent']),1+2*len(self.plan['policies']))
            create=[c for c in aws.call.call_args_list if c.args[1]=='create-role'][0]
            self.assertEqual(create.kwargs['MaxSessionDuration'],3600)
            self.assertEqual(create.kwargs['Path'],'/enmaas/')
            self.assertIn({'Key':'enmaas:group','Value':'demo'},create.kwargs['Tags'])
            self.assertFalse(any(c.args[1]=='create-access-key' for c in aws.call.call_args_list))

    def test_preflight_denial_collision_findings_and_quota_stop_before_writes(self):
        cases=('confirmation','account','root','role-exists','policy-exists','denied','finding','pagination','quota','role-quota','attachment-quota')
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                def response(service,action,**options):
                    if action=='get-caller-identity' and case=='account':return {'Account':'999999999999','Arn':'arn:aws:iam::999999999999:user/admin'}
                    if action=='get-caller-identity' and case=='root':return {'Account':'123456789012','Arn':'arn:aws:iam::123456789012:root'}
                    if action=='get-role' and case=='role-exists':return {'Role':{}}
                    if action=='get-policy' and case=='policy-exists':return {'Policy':{}}
                    if action=='get-role' and case=='denied':raise bootstrap.AwsFailure('AccessDenied')
                    if action=='validate-policy' and case=='finding':return {'findings':[{'findingType':'SECURITY_WARNING'}]}
                    if action=='validate-policy' and case=='pagination':return {'findings':[],'nextToken':'partial'}
                    result=self.response(service,action,**options)
                    if action=='get-account-summary':
                        if case=='quota':result['SummaryMap']['PoliciesQuota']=5
                        if case=='role-quota':result['SummaryMap']['RolesQuota']=5
                        if case=='attachment-quota':result['SummaryMap']['AttachedPoliciesPerRoleQuota']=1
                    return result
                aws=Mock();aws.call.side_effect=response;path=Path(tmp)/'bootstrap'
                with self.assertRaises(ValueError):self.apply(aws,path,'wrong' if case=='confirmation' else None)
                self.assertFalse(path.exists())
                self.assertFalse(any(c.args[1].startswith(('create-','attach-')) for c in aws.call.call_args_list))

    def test_every_write_failure_preserves_intent_and_never_automatically_retries(self):
        for failed in range(1+2*len(self.plan['policies'])):
            with self.subTest(write=failed),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'bootstrap';writes=[]
                def response(service,action,**options):
                    if action.startswith(('create-','attach-')):
                        writes.append(action)
                        self.assertEqual(json.loads(sorted(path.glob('*.json'))[-1].read_text())['event'],'intent')
                        if len(writes)==failed+1:raise bootstrap.AwsFailure('RequestTimeout')
                    return self.response(service,action,**options)
                aws=Mock();aws.call.side_effect=response
                with self.assertRaisesRegex(ValueError,'uncertain'):self.apply(aws,path)
                self.assertEqual(len(writes),failed+1)
                self.assertEqual(json.loads(sorted(path.glob('*.json'))[-1].read_text())['event'],'intent')

    def test_mismatched_created_identity_stops_before_attaching_permissions(self):
        for field in ('Arn','RoleId'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as tmp:
                def response(service,action,**options):
                    result=self.response(service,action,**options)
                    if action=='create-role':result['Role'][field]='unexpected'
                    return result
                aws=Mock();aws.call.side_effect=response
                with self.assertRaisesRegex(ValueError,'uncertain'):self.apply(aws,Path(tmp)/'bootstrap')
                self.assertEqual(aws.call.call_args.args[1],'create-role')

if __name__=='__main__':unittest.main()
