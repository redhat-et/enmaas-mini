"""Headless services must preflight every host and never replay uncertain work."""
import contextlib
import importlib.util
import io
import json
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('lab_services',(Path(__file__).resolve().parents[1] / 'services.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)


class Services(unittest.TestCase):
    def config(self):
        return {'version':1,'deployment':'em-lab-abcdef123456','revision':'a'*40,
            'repository':'/opt/enmaas-mini/em-lab-abcdef123456','source_repository':'https://github.com/redhat-et/enmaas-mini.git','ssh_key':'/tmp/lab-key',
            'known_hosts':'/tmp/known-hosts','tls_directory':'/tmp/lab-tls',
            'issuer_name':'identity.lab.example.com','workstation_cidr':'192.0.2.8/32',
            'external_database':False,'population_count':20,
            'hosts':{role:{'ssh':'ec2-user@192.0.2.'+str(i), 'private_ip':'10.42.0.'+str(i)}
                for i,role in enumerate(('keycloak','observability','gateway'),10)}}

    def test_offline_plan_uses_private_peers_and_existing_installers(self):
        with patch.object(s.subprocess,'run') as run:
            plan=s.plan(self.config())
        run.assert_not_called()
        ids=[step['id'] for step in plan]
        self.assertLess(ids.index('keycloak-install'),ids.index('observability-install'))
        self.assertLess(ids.index('gateway-install'),ids.index('telemetry-install'))
        text=json.dumps(plan)
        self.assertIn('--non-interactive',text);self.assertNotIn('--mock-profiles',text)
        self.assertIn('--issuer-name',text);self.assertIn('10.42.0.10',text)
        self.assertNotIn('0.0.0.0/0',text)
        self.assertLess(ids.index('population-create'),ids.index('population-transfer'))
        self.assertLess(ids.index('population-transfer'),ids.index('population-import'))
        self.assertLess(ids.index('gateway-install'),ids.index('personas-transfer'))
        self.assertLess(ids.index('personas-transfer'),ids.index('personas-configure'))
        population=next(step for step in plan if step['id']=='population-create')
        self.assertIn('20',population['argv'])
        self.assertNotIn('credentials.json',text)
        config=self.config();config['external_database']=True
        self.assertIn('--external-database-ca-file',json.dumps(s.plan(config)))

    def test_gateway_plan_is_accepted_by_the_real_installer(self):
        step=next(step for step in s.plan(self.config()) if step['id']=='gateway-install')
        # Parse the exact command with the installer, without reading secrets or hosts.
        args=s.installer.parse_args(step['argv'][2:]+['--plan'])
        self.assertEqual(args.mode,'mock')
        self.assertEqual(args.metering_revision,'2281e1a84bfc3b998e7db36bc01bae88130bfcdd')
        self.assertEqual(args.praxis_image,
            'quay.io/opendatahub/praxis-experimental@sha256:502f120fed7b8e516ce1d78396045c8f72673672351432b9dc2e61baf5c13ac8')
        self.assertEqual(args.praxis_revision,'5667c4acecd0981ae5114cbbf2ee3dcccec9c248')

    def test_access_summary_is_offline_and_scopes_all_commands(self):
        import shlex
        config=self.config()
        with patch.object(s.subprocess,'run') as run:
            summary=s.access_summary(config)
        run.assert_not_called()
        self.assertEqual(summary['urls']['pricetag'],'https://192.0.2.12:8443/login')
        self.assertEqual(summary['urls']['observability'],'https://192.0.2.11:8443/')
        self.assertEqual(summary['urls']['issuer'],'https://'+config['issuer_name']+':8443/realms/enmaas-lab')
        self.assertIn('not been checked',summary['status'])
        self.assertEqual(set(summary['browser_password_commands']),{'admin','alice','bob'})
        commands=[*summary['browser_password_commands'].values(),*summary['population_user_1'].values(),
                  summary['bootstrap_administrator_key'],summary['provider_inventory']]
        for command in commands:
            args=shlex.split(command)
            self.assertEqual(args[:3],['ssh','-F','/dev/null'])
            self.assertIn('StrictHostKeyChecking=yes',args);self.assertIn('ForwardAgent=no',args)
            self.assertIn('GlobalKnownHostsFile=/dev/null',args);self.assertIn('RequestTTY=no',args)
            # The checkout is entered inside the privileged context, not by the
            # non-root SSH account that cannot traverse the root-owned 0700 repo.
            self.assertTrue(args[-1].startswith('sudo -n sh -c '))
            self.assertIn('cd '+config['repository']+' && exec ',args[-1])
        self.assertNotIn('--stdout',shlex.split(summary['population_user_1']['metadata'])[-1])
        self.assertIn('--stdout',shlex.split(summary['population_user_1']['inference_key'])[-1])

    def test_provider_replacement_commands_are_interactive_and_gateway_scoped(self):
        import shlex
        config=self.config()
        with patch.object(s.subprocess,'run') as run:
            summary=s.access_summary(config)
        run.assert_not_called()
        replacement=summary['provider_replacement']
        self.assertIn('Stop',replacement['before_running'])
        for profile,flags in (
            ('openai',['openai','--replace']),
            ('external',['external','--api','both','--replace','--refresh-models'])):
            args=shlex.split(replacement[profile])
            self.assertIn('RequestTTY=yes',args)
            self.assertNotIn('RequestTTY=no',args)
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('ForwardAgent=no',args)
            self.assertEqual(args[-2],config['hosts']['gateway']['ssh'])
            self.assertEqual(shlex.split(args[-1]),
                ['sudo','-n','sh','-c','cd '+config['repository']+' && exec python3 '
                 'scripts/pricetag/providers '+' '.join(flags)])
            self.assertNotIn('--key-file',args[-1])
        self.assertIn('RequestTTY=no',shlex.split(summary['bootstrap_administrator_key']))

    def test_all_host_preflights_precede_first_write_and_completed_work_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state';calls=[]
            def preflight(config,role,fresh,source_ready=False):calls.append(('check',role,fresh))
            def execute(config,step):calls.append(('write',step['id']))
            with patch.object(s,'verify_source'),patch.object(s,'verify_issuer_resolution'),patch.object(s,'preflight',side_effect=preflight),patch.object(s,'execute',side_effect=execute):
                result=s.apply(self.config(),root,confirm='install em-lab-abcdef123456')
                self.assertEqual([x[0] for x in calls[:3]],['check']*3)
                self.assertTrue(all(x[2] for x in calls[:3]))
                self.assertEqual(result['completed_steps'],len(s.plan(self.config())))
                calls.clear();s.apply(self.config(),root,confirm='install em-lab-abcdef123456')
                self.assertEqual(len(calls),3);self.assertTrue(all(x[2] is False for x in calls))

    def test_resume_keeps_freshness_checks_until_each_hosts_tls_transfer(self):
        config=self.config();steps=s.plan(config)
        for completed_count in range(len(steps)+1):
            with self.subTest(completed_count=completed_count),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'state'
                with patch.object(s,'verify_source'),patch.object(s,'verify_issuer_resolution'), \
                     patch.object(s,'preflight'),patch.object(s,'execute'),contextlib.redirect_stdout(io.StringIO()):
                    s.apply(config,root,confirm='install '+config['deployment'])
                for step in steps[completed_count:]:
                    (root/(step['id']+'.json')).unlink()
                completed={step['id'] for step in steps[:completed_count]}
                with patch.object(s,'verify_source'),patch.object(s,'verify_issuer_resolution'), \
                     patch.object(s,'preflight') as preflight,patch.object(s,'execute'), \
                     contextlib.redirect_stdout(io.StringIO()):
                    s.apply(config,root,confirm='install '+config['deployment'])
                self.assertEqual(preflight.call_args_list,[
                    unittest.mock.call(config,role,fresh=role+'-tls' not in completed,
                                       source_ready=role+'-checkout' in completed)
                    for role in s.ROLES])

    def test_preflight_failure_never_starts_installation(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'verify_source'),patch.object(s,'verify_issuer_resolution'),patch.object(s,'preflight',side_effect=[None,ValueError('secret')]),patch.object(s,'execute') as execute:
            with self.assertRaises(ValueError):s.apply(self.config(),Path(tmp)/'state',confirm='install em-lab-abcdef123456')
            execute.assert_not_called()

    def test_uncertain_stage_is_not_replayed_and_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'state'
            with patch.object(s,'verify_source'),patch.object(s,'verify_issuer_resolution'),patch.object(s,'preflight'),patch.object(s,'execute',side_effect=RuntimeError('credential-sentinel')) as execute:
                with self.assertRaises(ValueError) as error:s.apply(self.config(),root,confirm='install em-lab-abcdef123456')
                self.assertNotIn('credential-sentinel',str(error.exception));self.assertEqual(execute.call_count,1)
                with self.assertRaises(ValueError):s.apply(self.config(),root,confirm='install em-lab-abcdef123456')
                self.assertEqual(execute.call_count,1)

    def test_config_rejects_unscoped_and_ambiguous_inputs(self):
        import copy
        bad=[None,dict(self.config(),unknown=True),dict(self.config(),version=True)]
        for field,value in (('deployment','foreign'),('revision','main'),('repository','/tmp/../other'),('repository','/opt/another-deployment'),
                            ('source_repository','https://user:secret@github.com/redhat-et/enmaas-mini.git'),('known_hosts','/tmp/%u'),('population_count',True),('population_count',0),('population_count',10001),('external_database','yes'),('workstation_cidr','0.0.0.0/0'),
                            ('workstation_cidr',123),('hosts',{}),('issuer_name','https://issuer.example')):
            bad.append(dict(self.config(),**{field:value}))
        for field,value in (('ssh','user@host'),('private_ip','203.0.113.2'),('private_ip',123)):
            config=copy.deepcopy(self.config());config['hosts']['gateway'][field]=value;bad.append(config)
        config=copy.deepcopy(self.config());config['hosts']['gateway']['extra']=True;bad.append(config)
        config=copy.deepcopy(self.config());config['hosts']['gateway']['ssh']=config['hosts']['keycloak']['ssh'];bad.append(config)
        for config in bad:
            with self.subTest(config=config),self.assertRaises(ValueError):s.plan(config)

    def test_confirmation_binding_and_noncontiguous_receipts_stop(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'verify_source'),patch.object(s,'verify_issuer_resolution'),patch.object(s,'preflight'),patch.object(s,'execute'):
            root=Path(tmp)/'state';config=self.config()
            with self.assertRaises(ValueError):s.apply(config,root,confirm='wrong')
            self.assertFalse(root.exists())
            with contextlib.redirect_stdout(io.StringIO()):s.apply(config,root,confirm='install '+config['deployment'])
            changed=dict(config,issuer_name='different.example.com')
            with self.assertRaises(ValueError):s.apply(changed,root,confirm='install '+config['deployment'])
            (root/(s.plan(config)[0]['id']+'.json')).unlink()
            with self.assertRaises(ValueError):s.apply(config,root,confirm='install '+config['deployment'])

    def test_outside_repository_command_keeps_arguments_without_checkout(self):
        config=self.config()
        with patch.object(s.transfer,'ssh_options',return_value=['ssh']), \
                patch.object(s.subprocess,'run',return_value=Mock(returncode=0)) as run:
            s.remote(config,'gateway',['dnf','install','-y','python3'],in_repository=False)
        self.assertEqual(run.call_args.args[0],
            ['ssh',config['hosts']['gateway']['ssh'],'sudo -n dnf install -y python3'])
        self.assertEqual(run.call_args.kwargs['timeout'],60)

    def test_execute_uses_fixed_transfers_and_bounded_remote_commands(self):
        config=self.config()
        with patch.object(s.certificates,'generate') as certificates,patch.object(s.transfer,'transfer_bundle') as bundle,patch.object(s.transfer,'transfer') as client,patch.object(s,'remote') as remote:
            for step in s.plan(config):s.execute(config,step)
            self.assertEqual(bundle.call_count,3);self.assertEqual(client.call_count,4)
            self.assertEqual(client.call_args.args[2],'population')
            self.assertEqual(certificates.call_args.args[0],Path(config['tls_directory']))
            self.assertEqual(certificates.call_args.kwargs['dns_names'],{'keycloak':config['issuer_name']})
            self.assertEqual(certificates.call_args.args[1]['observability'],['192.0.2.11','10.42.0.11'])
            self.assertTrue(all(call.kwargs['timeout']==5400 for call in remote.call_args_list))
            s.preflight(config,'gateway',True)
            self.assertEqual(remote.call_args.args[2][-3:],[config['revision'],'yes','no'])
            s.preflight(config,'gateway',False)
            self.assertEqual(remote.call_args.args[2][-2: ],['no','no'])
        with patch.object(s.transfer,'ssh_options',return_value=['ssh','-F','/dev/null']),patch.object(s.subprocess,'run',return_value=Mock(returncode=0)) as run:
            s.remote(config,'gateway',['python3','-c',"print('ok')"])
            self.assertEqual(run.call_args.kwargs['stdin'],s.subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stdout'],s.subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stderr'],s.subprocess.DEVNULL)
            self.assertIn('sudo -n sh -c',run.call_args.args[0][-1])
            self.assertIn('exec python3 -c',run.call_args.args[0][-1])
        for result in (Mock(returncode=1),OSError('credential-sentinel'),s.subprocess.TimeoutExpired('credential-sentinel',60)):
            with patch.object(s.transfer,'ssh_options',return_value=['ssh']),patch.object(s.subprocess,'run',side_effect=result if isinstance(result,Exception) else [result]):
                with self.assertRaises(ValueError) as caught:s.remote(config,'gateway',['true'])
                self.assertNotIn('credential-sentinel',str(caught.exception))

    def test_exact_remote_preflight_rejects_dirty_wrong_revision_and_existing_state(self):
        import os
        import platform
        import subprocess
        import sys
        from unittest.mock import MagicMock
        release=MagicMock();release.read_text.return_value='ID="rhel"\nVERSION_ID="9.6"'
        config=self.config()
        for revision,dirty,exists,success in ((config['revision'],'',False,True),('b'*40,'',False,False),
                                               (config['revision'],' M code',False,False),(config['revision'],'',True,False)):
            with patch('sys.argv',['preflight',config['repository'],config['revision'],'yes','yes']),patch.object(os,'geteuid',return_value=0), \
                 patch.object(platform,'machine',return_value='x86_64'),patch('pathlib.Path',return_value=release), \
                 patch.object(subprocess,'check_output',side_effect=[revision,dirty]),patch.object(os.path,'lexists',return_value=exists), \
                 patch.object(os,'lstat',return_value=Mock(st_mode=stat.S_IFDIR|0o755,st_uid=0)):
                if success:exec(s.PREFLIGHT,{})
                else:
                    with self.assertRaises(SystemExit):exec(s.PREFLIGHT,{})

    def test_cli_preview_apply_and_redacted_failure(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config.json';config.write_text(json.dumps(self.config()));config.chmod(0o600)
            base=[s.__file__,'--config',str(config)]
            with patch('sys.argv',base),contextlib.redirect_stdout(io.StringIO()) as out:s.main()
            self.assertFalse(json.loads(out.getvalue())['changed'])
            with patch('sys.argv',base+['--access']),contextlib.redirect_stdout(io.StringIO()) as out:s.main()
            self.assertIn('urls',json.loads(out.getvalue()))
            for extra in (['--confirm','anything'],['--state',str(root/'state')]):
                with patch('sys.argv',base+['--access',*extra]),self.assertRaises(ValueError):s.main()
            with patch('sys.argv',base+['--confirm','install '+self.config()['deployment']]),self.assertRaises(ValueError):s.main()
            with patch('sys.argv',base+['--confirm','install '+self.config()['deployment'],'--state',str(root/'state')]),patch.object(s,'apply',return_value={'completed_steps':18}) as apply,contextlib.redirect_stdout(io.StringIO()):s.main()
            self.assertEqual(apply.call_args.args[1],root/'state')
            result=subprocess.run([sys.executable,*base,'--confirm','bad'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,'')
            self.assertIn('no credential diagnostics',result.stderr)

    def test_checkout_fetches_exact_commit_without_worktree_secrets_or_overwrite(self):
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            def git(*argv):
                return subprocess.check_output(['git','-C',str(source),*argv],stderr=subprocess.DEVNULL,text=True).strip()
            git('init');(source/'README.md').write_text('reviewed source')
            git('add','README.md')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.test','-c','commit.gpgsign=false','commit','-m','fixture')
            revision=git('rev-parse','HEAD')
            (source/'private-untracked.key').write_text('must-not-transfer')
            destination=root/'installed'/'checkout'
            # Test the exact remote checkout flow with a local-only Git transport
            # and current UID. The production config accepts HTTPS GitHub only.
            script=s.CHECKOUT.replace('os.geteuid()!=0','os.geteuid()!=os.getuid()').replace('info.st_uid!=0','info.st_uid!=os.getuid()').replace('protocol.file.allow=never','protocol.file.allow=always')
            command=[sys.executable,'-c',script,str(destination),str(source),revision]
            result=subprocess.run(command,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((destination/'README.md').read_text(),'reviewed source')
            self.assertFalse((destination/'private-untracked.key').exists())
            actual=subprocess.check_output(['git','-C',str(destination),'rev-parse','HEAD'],text=True).strip()
            self.assertEqual(actual,revision)
            before=(destination/'.git/HEAD').read_bytes()
            retry=subprocess.run(command,capture_output=True)
            self.assertNotEqual(retry.returncode,0)
            self.assertEqual((destination/'.git/HEAD').read_bytes(),before)
            wrong=root/'installed'/'wrong'
            failure=subprocess.run([sys.executable,'-c',script,str(wrong),str(source),'b'*40],capture_output=True)
            self.assertNotEqual(failure.returncode,0);self.assertTrue(wrong.exists())
            self.assertFalse((wrong/'README.md').exists())
            link=root/'redirect';link.symlink_to(root/'installed',target_is_directory=True)
            denied=subprocess.run([sys.executable,'-c',script,str(link/'other'),str(source),revision],capture_output=True)
            self.assertNotEqual(denied.returncode,0);self.assertFalse((root/'installed/other').exists())

    def test_issuer_resolution_is_bounded_exact_and_fails_before_remote_writes(self):
        config=self.config()
        with patch.object(s.subprocess,'run',return_value=Mock(returncode=0,stdout='["192.0.2.10"]')) as run:
            s.verify_issuer_resolution(config)
        self.assertEqual(run.call_args.kwargs['timeout'],10)
        self.assertEqual(run.call_args.args[0][-1],config['issuer_name'])
        import socket
        import sys
        resolved=io.StringIO()
        with patch.object(sys,'argv',['resolver',config['issuer_name']]), \
             patch.object(socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.10',8443))]*2) as lookup, \
             contextlib.redirect_stdout(resolved):
            exec(run.call_args.args[0][3],{})
        self.assertEqual(json.loads(resolved.getvalue()),['192.0.2.10'])
        lookup.assert_called_once_with(config['issuer_name'],8443,type=socket.SOCK_STREAM)
        for result in (Mock(returncode=1,stdout=''),Mock(returncode=0,stdout='[]'),
                       Mock(returncode=0,stdout='["192.0.2.99"]'),
                       Mock(returncode=0,stdout='["192.0.2.10", "2001:db8::1"]'),
                       Mock(returncode=0,stdout='not-json'),Mock(returncode=0,stdout='{}'),
                       Mock(returncode=0,stdout='x'*4097),
                       OSError('private-diagnostic'),s.subprocess.TimeoutExpired('resolver',10)):
            with patch.object(s.subprocess,'run',side_effect=result if isinstance(result,Exception) else [result]),self.assertRaises(ValueError):
                s.verify_issuer_resolution(config)
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'verify_source'), \
             patch.object(s,'verify_issuer_resolution',side_effect=ValueError('wrong DNS')),patch.object(s,'preflight') as remote:
            root=Path(tmp)/'state'
            with self.assertRaises(ValueError):s.apply(config,root,confirm='install '+config['deployment'])
            self.assertFalse(root.exists());remote.assert_not_called()

    def test_coordinator_must_match_the_clean_reviewed_source_revision(self):
        config=self.config()
        clean=Mock(returncode=0,stdout='');head=Mock(returncode=0,stdout=config['revision']+'\n')
        with patch.object(s.subprocess,'run',side_effect=[head,clean]) as run:s.verify_source(config)
        self.assertTrue(all(call.kwargs['timeout']==15 for call in run.call_args_list))
        for result in ([Mock(returncode=1,stdout=''),clean],[head,Mock(returncode=1,stdout='')],
                       [Mock(returncode=0,stdout='b'*40),clean],[head,Mock(returncode=0,stdout=' M services.py')],
                       OSError('credential-sentinel'),s.subprocess.TimeoutExpired('credential-sentinel',15)):
            with patch.object(s.subprocess,'run',side_effect=result),self.assertRaises(ValueError) as caught:s.verify_source(config)
            self.assertNotIn('credential-sentinel',str(caught.exception))
