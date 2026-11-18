"""Client transfers never pass secret bytes through argv or print failed SSH output."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('transfer_client',(Path(__file__).resolve().parents[1] / 'transfer-client.py'))
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)


class Transfer(unittest.TestCase):
    def inputs(self,root):
        key=root/'key';known=root/'known hosts';key.write_text('placeholder');known.write_text('verified fixture');key.chmod(0o600);known.chmod(0o600)
        return ['ec2-user@192.0.2.1','ec2-user@192.0.2.2','gateway',key,known]

    def test_verified_ssh_and_secret_only_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=self.inputs(Path(tmp))
            for kind in ('gateway','observability'):
                args[2]=kind
                with patch.object(client.subprocess,'run',side_effect=[Mock(returncode=0,stdout=b'credential-sentinel-unique'),Mock(returncode=0,stdout=b'')]) as run:
                    result=client.transfer(*args)
                self.assertEqual(result['client'],kind)
                source,dest=run.call_args_list
                self.assertNotIn('credential-sentinel-unique',repr(dest.args))
                self.assertEqual(dest.kwargs['input'],b'credential-sentinel-unique')
                self.assertEqual(source.kwargs['stdin'],subprocess.DEVNULL)
                self.assertIn('StrictHostKeyChecking=yes',dest.args[0])
                self.assertIn('ForwardAgent=no',dest.args[0])
                self.assertIn('UserKnownHostsFile="'+str(args[4])+'"',dest.args[0])
                self.assertIn('GlobalKnownHostsFile=/dev/null',dest.args[0])
                self.assertEqual(shlex.split(dest.args[0][-1])[-1],client.FILES[kind][1])

    def test_population_transfer_validates_export_and_never_sends_passwords(self):
        profile={'user_id':'8beb9bf1-7cb8-47fa-ac79-a72947000376',
            'tags':{'email':'perf-aaaaaaaaaaaaaaaa-00001@example.test','first_name':'Synthetic','last_name':'User'}}
        document={'version':1,'population':'perf-aaaaaaaaaaaaaaaa','profiles':[profile]}
        with tempfile.TemporaryDirectory() as tmp:
            args=self.inputs(Path(tmp));args[2]='population'
            for bad in (b'not-json',json.dumps(dict(document,password='private-sentinel')).encode(),
                        json.dumps(dict(document,profiles=[dict(profile,password='private-sentinel')])).encode(),
                        b'x'*(8*1024*1024+1)):
                with patch.object(client.subprocess,'run',return_value=Mock(returncode=0,stdout=bad)) as run,self.assertRaises(ValueError):
                    client.transfer(*args)
                self.assertEqual(run.call_count,1)
            raw=json.dumps(document).encode()
            with patch.object(client.subprocess,'run',side_effect=[Mock(returncode=0,stdout=raw),Mock(returncode=0,stdout=b'')]) as run:
                result=client.transfer(*args)
            self.assertEqual(result['path'],'/root/enmaas-inputs/population-profiles.json')
            self.assertEqual(run.call_args.kwargs['input'],raw)
            self.assertIn('8388608',run.call_args_list[0].args[0][-1])
            self.assertNotIn('perf-aaaaaaaaaaaaaaaa',repr(run.call_args.args))

    def test_personas_transfer_accepts_only_three_distinct_uuids(self):
        document={name:str(uuid.uuid4()) for name in ('admin','alice','bob')}
        with tempfile.TemporaryDirectory() as tmp:
            args=self.inputs(Path(tmp));args[2]='personas'
            for bad in ({},dict(document,password='must-not-transfer'),dict(document,bob=document['alice'])):
                with patch.object(client.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(bad).encode())) as run:
                    with self.assertRaises(ValueError):client.transfer(*args)
                self.assertEqual(run.call_count,1)
            raw=json.dumps(document).encode()
            with patch.object(client.subprocess,'run',side_effect=[Mock(returncode=0,stdout=raw),Mock(returncode=0,stdout=b'')]) as run:
                result=client.transfer(*args)
            self.assertEqual(result['path'],'/root/enmaas-inputs/personas.json')
            self.assertEqual(run.call_args.kwargs['input'],raw)

    def test_ten_thousand_profile_export_fits_reviewed_transfer_limit(self):
        prefix='perf-'+'a'*16
        document={'version':1,'population':prefix,'profiles':[
            {'user_id':str(uuid.UUID(int=i)),'tags':{'email':prefix+'-'+str(i).zfill(5)+'@example.test',
            'first_name':'Synthetic','last_name':'User'}} for i in range(1,10001)]}
        raw=json.dumps(document).encode()
        client.population.validate(document)
        self.assertGreater(len(raw),65536);self.assertLess(len(raw),8*1024*1024)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest=root/'profiles.json'
            scripts=[code.replace('65537',str(8*1024*1024+1)).replace('65536',str(8*1024*1024))
                .replace('os.geteuid()!=0','os.geteuid()!=os.getuid()').replace('s.st_uid!=0','s.st_uid!=os.getuid()')
                for code in (client.WRITE,client.READ)]
            write=subprocess.run([sys.executable,'-c',scripts[0],str(dest)],input=raw,capture_output=True)
            self.assertEqual(write.returncode,0);self.assertEqual(write.stdout,b'')
            read=subprocess.run([sys.executable,'-c',scripts[1],str(dest)],capture_output=True)
            self.assertEqual(read.returncode,0);self.assertEqual(read.stdout,raw)

    def test_source_failure_never_writes_destination_and_redacts_every_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=self.inputs(Path(tmp))
            for response in [Mock(returncode=1,stdout=b'credential-sentinel-unique',stderr=b'credential-sentinel-unique'),OSError('secret'),subprocess.TimeoutExpired('secret',45)]:
                effect=response if isinstance(response,Exception) else [response]
                with patch.object(client.subprocess,'run',side_effect=effect) as run,self.assertRaises(ValueError) as caught:
                    client.transfer(*args)
                self.assertNotIn("b'credential-sentinel-unique'",str(caught.exception));self.assertEqual(run.call_count,1)
            for payload in [b'',b'x'*65537]:
                with patch.object(client.subprocess,'run',return_value=Mock(returncode=0,stdout=payload)) as run,self.assertRaisesRegex(ValueError,'empty or oversized'):
                    client.transfer(*args)
                self.assertEqual(run.call_count,1)
            with patch.object(client.subprocess,'run',side_effect=[Mock(returncode=0,stdout=b'credential-sentinel-unique'),Mock(returncode=1,stdout=b'credential-sentinel-unique')]),self.assertRaises(ValueError):
                client.transfer(*args)

    def test_bad_host_and_local_file_inputs_stop_before_ssh(self):
        with self.assertRaises(ValueError):client.private_file('/tmp/%u')
        for name in ['--option','user@host','user@127.0.0.1;echo x']:
            with self.assertRaises(ValueError):client.target(name)
        with tempfile.TemporaryDirectory() as tmp,patch.object(client.subprocess,'run') as run:
            root=Path(tmp);args=self.inputs(root)
            with self.assertRaises(ValueError):client.transfer(args[0],args[0],*args[2:])
            bad=list(args);bad[2]='invalid'
            with self.assertRaises(ValueError):client.transfer(*bad)
            args[3].chmod(0o644)
            with self.assertRaises(ValueError):client.transfer(*args)
            args[3].unlink();args[3].symlink_to(args[4])
            with self.assertRaises(ValueError):client.transfer(*args)
            run.assert_not_called()

    def test_remote_scripts_atomic_write_no_overwrite_and_source_validation(self):
        # Exercise the exact remote code using the current test UID in place of root.
        # SSH always sends the original script, which requires root ownership.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest=root/'secret'
            read=client.READ.replace('os.geteuid()!=0','os.geteuid()!=os.getuid()').replace('s.st_uid!=0','s.st_uid!=os.getuid()')
            write=client.WRITE.replace('os.geteuid()!=0','os.geteuid()!=os.getuid()').replace('s.st_uid!=0','s.st_uid!=os.getuid()')
            def call(script,path,data=None):return subprocess.run([sys.executable,'-c',script,str(path)],input=data,capture_output=True)
            for data in [b'',b'x'*65537]:
                self.assertNotEqual(call(write,dest,data).returncode,0);self.assertFalse(dest.exists())
            self.assertEqual(call(write,dest,b'credential-sentinel-unique').returncode,0)
            self.assertEqual(dest.stat().st_mode&0o777,0o600)
            self.assertNotEqual(call(write,dest,b'replacement').returncode,0)
            self.assertEqual(dest.read_bytes(),b'credential-sentinel-unique');self.assertEqual(list(root.glob('.client-*')),[])
            self.assertEqual(call(read,dest).stdout,b'credential-sentinel-unique')
            dest.chmod(0o644);self.assertNotEqual(call(read,dest).returncode,0)
            dest.unlink();dest.symlink_to(root/'elsewhere')
            self.assertNotEqual(call(write,dest,b'credential-sentinel-unique').returncode,0);self.assertFalse((root/'elsewhere').exists())
            root.chmod(0o755);self.assertNotEqual(call(write,root/'other',b'credential-sentinel-unique').returncode,0)

    def test_role_bundle_selects_only_allowlisted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=self.inputs(root);bundle=root/'gateway';bundle.mkdir(mode=0o700)
            for name in client.BUNDLES['gateway']:
                (bundle/name).write_text('leaf-material');(bundle/name).chmod(0o600)
            (bundle/'issuer-ca.key').write_text('signing-secret-must-not-transfer')
            with patch.object(client.subprocess,'run',return_value=Mock(returncode=0,stdout=b'')) as run:
                result=client.transfer_bundle(args[1],'gateway',bundle,args[3],args[4])
            self.assertEqual(result['path'],'/root/enmaas-tls')
            payload=run.call_args.kwargs['input']
            self.assertNotIn(b'signing-secret',payload)
            self.assertEqual(set(json.loads(payload)),set(client.BUNDLES['gateway']))
            self.assertNotIn('leaf-material',repr(run.call_args.args))
            for bad in (Mock(returncode=1),OSError('secret'),subprocess.TimeoutExpired('secret',45)):
                with patch.object(client.subprocess,'run',side_effect=bad if isinstance(bad,Exception) else [bad]),self.assertRaises(ValueError) as caught:
                    client.transfer_bundle(args[1],'gateway',bundle,args[3],args[4])
                self.assertNotIn('secret',str(caught.exception))
            with self.assertRaises(ValueError):client.transfer_bundle(args[1],'authorities',bundle,args[3],args[4])
            leaf=bundle/'server.key'
            leaf.chmod(0o644)
            with self.assertRaises(ValueError),patch.object(client.subprocess,'run') as run:
                client.transfer_bundle(args[1],'gateway',bundle,args[3],args[4])
            run.assert_not_called();leaf.chmod(0o600)
            for value in ('','x'*65537):
                leaf.write_text(value)
                with self.assertRaises(ValueError),patch.object(client.subprocess,'run') as run:
                    client.transfer_bundle(args[1],'gateway',bundle,args[3],args[4])
                run.assert_not_called()
            leaf.unlink();leaf.symlink_to(bundle/'issuer-ca.key')
            with self.assertRaises(OSError),patch.object(client.subprocess,'run') as run:
                client.transfer_bundle(args[1],'gateway',bundle,args[3],args[4])
            run.assert_not_called()

    def test_remote_bundle_refuses_overwrites_and_unsafe_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest=root/'tls'
            script=client.BUNDLE_WRITE.replace('os.geteuid()!=0','os.geteuid()!=os.getuid()').replace('s.st_uid!=0','s.st_uid!=os.getuid()')
            names=client.BUNDLES['keycloak'];payload={name:'leaf' for name in names}
            def call(data):
                return subprocess.run([sys.executable,'-c',script,str(dest),json.dumps(names)],input=json.dumps(data).encode(),capture_output=True)
            for bad in ({'../escape':'bad'},dict(payload,unknown='bad'),dict(payload,**{'server.key':''}),dict(payload,**{'server.key':123})):
                self.assertNotEqual(call(bad).returncode,0);self.assertFalse(dest.exists())
            self.assertEqual(call(payload).returncode,0)
            self.assertEqual(dest.stat().st_mode&0o777,0o700)
            self.assertTrue(all(x.stat().st_mode&0o777==0o600 for x in dest.iterdir()))
            self.assertNotEqual(call(payload).returncode,0)
            self.assertEqual((dest/'server.key').read_text(),'leaf')

    def test_bundle_cli_selects_mode_without_ambiguous_inputs(self):
        base=[str(Path(client.__file__)),'--destination','ec2-user@192.0.2.2',
              '--ssh-key','/tmp/key','--known-hosts','/tmp/hosts']
        for suffix in ([],['--role','gateway'],['--bundle','/tmp/tls'],
                       ['--role','gateway','--bundle','/tmp/tls','--issuer','ec2-user@192.0.2.1'],
                       ['--role','gateway','--bundle','/tmp/tls','--client','gateway']):
            with patch('sys.argv',base+suffix),self.assertRaises(ValueError):client.main()
        result={'client':'gateway TLS','destination':'ec2-user@192.0.2.2','path':'/root/enmaas-tls'}
        with patch('sys.argv',base+['--role','gateway','--bundle','/tmp/tls']), \
             patch.object(client,'transfer_bundle',return_value=result) as run,contextlib.redirect_stdout(io.StringIO()) as out:
            client.main()
        self.assertEqual(run.call_args.args[1],'gateway')
        self.assertIn('gateway TLS',out.getvalue())

    def test_cli_safe_success_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=self.inputs(root)
            argv=[str(Path(client.__file__)),'--issuer',args[0],'--destination',args[1],'--client',args[2],
                  '--ssh-key',str(args[3]),'--known-hosts',str(args[4])]
            with patch('sys.argv',argv),patch.object(client,'transfer',return_value={'client':'gateway','destination':args[1],'path':'/root/enmaas-inputs/oidc.json'}),contextlib.redirect_stdout(io.StringIO()) as out:
                client.main()
            self.assertIn('Installed gateway client file',out.getvalue())
            args[3].unlink()
            result=subprocess.run([sys.executable,*argv],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('No credential values printed',result.stderr)


if __name__=='__main__':unittest.main()
