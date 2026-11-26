"""Combined network/host launch must fail closed and retain recovery evidence."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('infrastructure',(Path(__file__).resolve().parents[1] / 'infrastructure.py'))
infra=importlib.util.module_from_spec(spec);spec.loader.exec_module(infra)


class Infrastructure(unittest.TestCase):
    def manifest(self):
        return infra.registry.new('123456789012','eu-central-1','/tmp/labkey','operator',group='lab')

    def options(self,root):
        return dict(cidr='10.42.0.0/20',zones=['eu-central-1a','eu-central-1b'],
            allowed_cidr='192.0.2.1/32',image='ami-12345678',journal=root/'run')

    def test_preview_is_offline_and_restricted(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(infra.subprocess,'run') as command:
            m=self.manifest();p=infra.preview(m,**self.options(Path(tmp)))
            command.assert_not_called()
            self.assertFalse(p['runtime_verified'])
            self.assertEqual(list(p['hosts']),['gateway','keycloak','observability'])
            self.assertEqual(p['hosts']['gateway']['instance_type'],'m7i.2xlarge')
            self.assertEqual(p['hosts']['keycloak']['volume_gib'],50)
            self.assertEqual(p['hosts']['observability']['volume_gib'],100)
            for h in p['hosts'].values():
                self.assertEqual(h['https_access'],'restricted')
            self.assertFalse((Path(tmp)/'run').exists())
            for field,value in [('allowed_cidr','0.0.0.0/0'),('image','latest'),('zones',[])]:
                options=self.options(Path(tmp));options[field]=value
                with self.assertRaises(ValueError):infra.preview(m,**options)
            m['zone_id']='ZEXAMPLE';m['domain']='example.com'
            plan=infra.preview(m,**self.options(Path(tmp)))
            self.assertEqual(plan['dns'],{'zone_id':'ZEXAMPLE','domain':'example.com','records_created':False})

    def test_confirmation_and_bad_key_precede_any_cloud_mutation(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(infra.network,'create') as network:
            m=self.manifest();opts=self.options(Path(tmp))
            with self.assertRaisesRegex(ValueError,'confirmation'):
                infra.deploy(m,confirm='wrong',**opts)
            with self.assertRaisesRegex(ValueError,'public key'):
                infra.deploy(m,confirm='deploy '+m['id'],**opts)
            network.assert_not_called();self.assertFalse(opts['journal'].exists())

    def test_public_key_validation_redacts_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'key.pub'
            for content in ['', 'PRIVATE KEY', 'ssh-ed25519 a\nssh-ed25519 b','bad-key a']:
                p.write_text(content)
                with self.assertRaises(ValueError):infra.check_public_key(p)
            p.write_text('ssh-ed25519 AAAA label\n')
            with patch.object(infra.subprocess,'run',return_value=Mock(returncode=0)) as run:
                infra.check_public_key(p)
                self.assertTrue(run.call_args.kwargs['capture_output'])
            with patch.object(infra.subprocess,'run',return_value=Mock(returncode=1)):
                with self.assertRaisesRegex(ValueError,'public key'):infra.check_public_key(p)

    def test_all_hosts_planned_before_first_launch_and_private_journals(self):
        self.run_deploy()

    def run_deploy(self,fail_plan=None,fail_launch=None,wrong_vpc=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m=self.manifest();opts=self.options(root);calls=[]
            def plan(client,args):
                calls.append('plan:'+args.scenario)
                if args.scenario==fail_plan:raise ValueError('plan failed')
                return {'VpcId':'vpc-87654321' if wrong_vpc else 'vpc-12345678'}
            def launch(client,args,proposal):
                calls.append('launch:'+args.scenario)
                if args.scenario==fail_launch:raise ValueError('launch failed')
                infra.vm.save_state(args.state_file,{'InstanceId':'i-12345678','ApplyStatus':'launched'})
            resources={'vpc':'vpc-12345678','subnets':{'services-1':'subnet-12345678','services-2':'subnet-87654321'}}
            with patch.object(infra,'check_public_key'),patch.object(infra.network,'create',return_value=resources), \
                 patch.object(infra.vm,'plan',side_effect=plan),patch.object(infra.vm,'apply_plan',side_effect=launch):
                if fail_plan or fail_launch or wrong_vpc:
                    with self.assertRaises(ValueError):infra.deploy(m,confirm='deploy '+m['id'],**opts)
                else:
                    result=infra.deploy(m,confirm='deploy '+m['id'],**opts)
                    self.assertEqual(len(result['hosts']),3)
                    self.assertFalse(result['runtime_verified'])
                    self.assertEqual(calls[:3],['plan:remote-gateway','plan:keycloak','plan:observability'])
                    for role in infra.HOSTS:
                        path=opts['journal']/(m['id']+'-'+role+'.json')
                        self.assertEqual(path.stat().st_mode&0o777,0o600)
                    with self.assertRaises(FileExistsError):infra.deploy(m,confirm='deploy '+m['id'],**opts)
            events=[json.loads(p.read_text()) for p in sorted(opts['journal'].glob('[0-9]*.json'))]
            if fail_plan or wrong_vpc:self.assertFalse(any(x.startswith('launch:') for x in calls))
            if fail_launch:
                self.assertEqual(events[-1],{'event':'host-launch-intent','role':'keycloak','journal':str(opts['journal']/(m['id']+'-keycloak.json'))})
                self.assertTrue((opts['journal']/(m['id']+'-gateway.json')).exists())
                self.assertNotIn('launch:observability',calls)

    def test_failures_leave_evidence_without_launching_more_hosts(self):
        self.run_deploy(fail_plan='observability')
        self.run_deploy(fail_launch='keycloak')
        self.run_deploy(wrong_vpc=True)

    def test_new_deployment_preview_and_apply_register_before_cloud_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=['infrastructure.py','--group','lab','--account','123456789012',
                '--profile','operator','--ssh-key','/tmp/key','--registry-dir',str(root/'registry'),
                '--cidr','10.42.0.0/20','--zone','eu-central-1a','--zone','eu-central-1b',
                '--allowed-cidr','192.0.2.1/32','--image','ami-12345678','--journal',str(root/'run')]
            with patch('sys.argv',args),contextlib.redirect_stdout(io.StringIO()) as out:
                infra.main()
            result=json.loads(out.getvalue())
            self.assertEqual(result['region'],'eu-central-1')
            self.assertFalse(result['registered'])
            self.assertFalse((root/'registry').exists())
            args += ['--dns-zone-id','ZEXAMPLE','--dns-domain','example.com']
            with patch('sys.argv',args),contextlib.redirect_stdout(io.StringIO()) as out:
                infra.main()
            self.assertEqual(json.loads(out.getvalue())['dns'],
                {'zone_id':'ZEXAMPLE','domain':'example.com','records_created':False})
            for flag in ('--dns-zone-id','--dns-domain'):
                broken=list(args);index=broken.index(flag);del broken[index:index+2]
                with patch('sys.argv',broken),self.assertRaises(ValueError):infra.main()
            with patch('sys.argv',args+['--apply','--confirm','wrong']),self.assertRaises(ValueError):infra.main()
            self.assertFalse((root/'registry').exists())
            def apply(manifest,**options):
                saved=infra.registry.load(root/'registry'/(manifest['id']+'.json'))
                self.assertEqual(saved,manifest)
                self.assertEqual(saved['zone_id'],'ZEXAMPLE')
                self.assertEqual(saved['domain'],'example.com')
                self.assertEqual(options['confirm'],'deploy '+manifest['id'])
                return {'deployment':manifest['id']}
            with patch('sys.argv',args+['--apply','--confirm','deploy lab 123456789012']), \
                 patch.object(infra,'check_public_key'),patch.object(infra,'deploy',side_effect=apply),contextlib.redirect_stdout(io.StringIO()) as out:
                infra.main()
            self.assertIn('Recovery manifest:',out.getvalue())
            self.assertEqual(len(list((root/'registry').glob('*.json'))),1)
            (root/'run').mkdir()
            with patch('sys.argv',args+['--apply','--confirm','deploy lab 123456789012']),self.assertRaises(FileExistsError):infra.main()
            self.assertEqual(len(list((root/'registry').glob('*.json'))),1)
            for flag in ('--account','--profile','--ssh-key'):
                broken=list(args);index=broken.index(flag);del broken[index:index+2]
                with patch('sys.argv',broken),self.assertRaises(ValueError):infra.main()

    def test_cli_preview_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m=self.manifest();manifest=infra.registry.save(root,m)
            args=['infrastructure.py','--manifest',str(manifest),'--cidr','10.42.0.0/20',
                  '--zone','eu-central-1a','--zone','eu-central-1b','--allowed-cidr','192.0.2.1/32',
                  '--image','ami-12345678','--journal',str(root/'run')]
            with patch('sys.argv',args),contextlib.redirect_stdout(io.StringIO()) as out:
                infra.main()
            self.assertFalse(json.loads(out.getvalue())['runtime_verified'])
            with patch('sys.argv',args+['--profile','other']),self.assertRaisesRegex(ValueError,'do not override'):
                infra.main()
            for flag in ('--dns-zone-id','--dns-domain'):
                with patch('sys.argv',args+[flag,'new']),self.assertRaisesRegex(ValueError,'do not override'):
                    infra.main()
            command=[sys.executable,str(Path(infra.__file__)),*args[1:]]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(json.loads(result.stdout)['runtime_verified'])
            result=subprocess.run(command+['--apply'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('confirmation',result.stderr)
            with patch('sys.argv',args+['--apply','--confirm','deploy '+m['id']]), \
                 patch.object(infra,'deploy',return_value={'ok':True}) as deploy,contextlib.redirect_stdout(io.StringIO()):
                infra.main();deploy.assert_called_once()


if __name__=='__main__':unittest.main()
