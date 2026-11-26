#!/usr/bin/env python3
"""Cloud-free VM CLI, credential-source and AWS adapter boundaries."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader('vm_boundaries', str(ROOT/'scripts/aws/rhel-vm'))
spec = importlib.util.spec_from_loader(loader.name, loader)
vm = importlib.util.module_from_spec(spec)
loader.exec_module(vm)


class AwsAdapter(unittest.TestCase):
    def test_serialization_profile_and_boolean_flags(self):
        aws = vm.Aws('eu-central-1', 'scoped-role', False)
        with patch.object(vm.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{}')) as run:
            aws.call('ec2', 'describe-key-pairs', include_public_key=True, dry_run=False,
                     key_names=['test-key'], filters=[{'Name':'tag:owner','Values':['test']}], max_results=10)
            command = run.call_args.args[0]
            self.assertIn('--include-public-key', command)
            self.assertIn('--no-dry-run', command)
            self.assertEqual(command[command.index('--profile')+1], 'scoped-role')
            self.assertEqual(json.loads(command[command.index('--key-names')+1]), ['test-key'])
            self.assertEqual(command[command.index('--max-results')+1], '10')
            self.assertTrue(run.call_args.kwargs['capture_output'])
            self.assertEqual(run.call_args.kwargs['env']['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
            self.assertNotIn('AWS_ACCESS_KEY_ID',run.call_args.kwargs['env'])

    def test_errors_are_redacted_and_unknown_mutations_refused(self):
        aws = vm.Aws('eu-central-1', None, True)
        with patch.object(vm.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'allowlist'):
                aws.call('ec2', 'terminate-instances')
            run.assert_not_called()
            for stderr, expected in (('Error (AccessDenied) synthetic-private-value', 'AccessDenied'),
                                     ('synthetic-private-value', 'CLI error')):
                run.return_value = SimpleNamespace(returncode=1, stderr=stderr, stdout='')
                with self.assertRaises(vm.AwsError) as error:
                    aws.call('ec2', 'describe-instances')
                self.assertEqual(error.exception.code, expected)
                self.assertNotIn('synthetic-private-value', str(error.exception))

    def test_timeout_is_bounded_redacted_and_never_retried(self):
        for action in ('describe-instances','run-instances'):
            error=subprocess.TimeoutExpired(['aws','synthetic-private-value'],120,
                                            output='private-output',stderr='private-error')
            with self.subTest(action=action),patch.object(vm.subprocess,'run',side_effect=error) as run:
                with self.assertRaises(vm.AwsError) as caught:
                    vm.Aws('eu-central-1','scoped-role',True).call('ec2',action)
                self.assertEqual(caught.exception.code,'Timeout')
                self.assertIn('journal',str(caught.exception))
                self.assertNotIn('private',str(caught.exception))
                self.assertEqual(run.call_count,1)
                self.assertEqual(run.call_args.kwargs['timeout'],120)


class VMCLI(unittest.TestCase):
    def setUp(self):
        self.base = ['--region', 'eu-central-1', '--account-id', '123456789012']
        self.environment = {k:v for k,v in os.environ.items() if not k.startswith('AWS_')}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def main(self, mode='plan', *flags):
        with patch.object(vm.sys, 'argv', ['rhel-vm', mode, *self.base, *flags]):
            vm.main()

    def test_invalid_account_or_mixed_credentials_never_create_client(self):
        cases = [({'AWS_ACCESS_KEY_ID':'dummy'}, []),
                 ({'AWS_SECRET_ACCESS_KEY':'dummy'}, []),
                 ({'AWS_ACCESS_KEY_ID':'dummy','AWS_SECRET_ACCESS_KEY':'dummy'}, ['--profile','role']),
                 ({'AWS_ACCESS_KEY_ID':'dummy','AWS_SECRET_ACCESS_KEY':'dummy','AWS_PROFILE':'role'}, []),
                 ({}, ['--account-id','wrong'])]
        for environment, flags in cases:
            with self.subTest(environment=list(environment),flags=flags), patch.dict(os.environ, environment), patch.object(vm, 'Aws') as client:
                with self.assertRaises(ValueError):self.main('plan', *flags)
                client.assert_not_called()

    def test_special_commands_require_explicit_inputs(self):
        for mode in ('capacity', 'vllm-endpoint', 'vllm-grant'):
            with self.subTest(mode=mode), patch.object(vm, 'Aws') as client:
                with self.assertRaises(ValueError):self.main(mode)
                client.return_value.call.assert_not_called()
        with patch.object(vm, 'Aws') as client:
            with self.assertRaises(SystemExit):self.main('verify')
            client.return_value.call.assert_not_called()

    def test_special_commands_dispatch_readonly_and_lock_grants(self):
        with patch.object(vm, 'Aws') as client, patch.object(vm, 'verify') as verify:
            self.main('verify', '--state-file', '/tmp/state')
            verify.assert_called_once()
            client.assert_called_once_with('eu-central-1', None, False)
        with patch.object(vm, 'Aws'), patch.object(vm, 'vllm_endpoint') as endpoint:
            self.main('vllm-endpoint', '--client-state-file', '/tmp/client', '--client-vpc-id', 'vpc-test')
            endpoint.assert_called_once()
        for apply in (False, True):
            with self.subTest(apply=apply), patch.object(vm, 'Aws'), patch.object(vm, 'vllm_grant') as grant, patch.object(vm, 'deployment_lock') as lock:
                self.main('vllm-grant', '--client-state-file', '/tmp/client', '--vllm-state-file', '/tmp/vllm', *(['--apply'] if apply else []))
                grant.assert_called_once()
                self.assertEqual(lock.call_count, int(apply))

    def test_plan_requires_complete_configuration_and_bounded_names(self):
        def configure(args):
            args.scenario = 'remote-gateway'
            args.prefix = 'test-run'
            args.subnet_id = 'subnet-test'
            args.public_key = Path('/tmp/public-key')
            args.volume_gib = 50
        base = ['--state-file', '/tmp/state', '--config', '/tmp/config']
        for mode in ('missing', 'bad-prefix', 'small-disk', 'valid'):
            def settings(args):
                configure(args)
                if mode=='missing':args.subnet_id=None
                if mode=='bad-prefix':args.prefix='../foreign'
                if mode=='small-disk':args.volume_gib=49
            with self.subTest(mode=mode), patch.object(vm, 'Aws'), patch.object(vm, 'configure', side_effect=settings), patch.object(vm, 'execute_plan') as execute:
                if mode=='valid':
                    self.main('plan', *base)
                    execute.assert_called_once()
                else:
                    with self.assertRaises(ValueError):self.main('plan', *base)
                    execute.assert_not_called()

    def test_cli_error_reports_no_automatic_cleanup(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/aws/rhel-vm'), 'plan',
                                 '--region', 'eu-central-1', '--account-id', 'invalid'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No automatic cleanup', result.stderr)


class LaunchOrchestration(unittest.TestCase):
    def test_plan_never_prompts_or_applies_for_all_access_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            for inference in ('none','cpu','gpu'):
                for public in (False,True):
                    args=SimpleNamespace(state_file=Path(tmp)/'absent',mode='plan',
                        ssh_access='public' if public else 'restricted',
                        https_access='public' if public else 'restricted',scenario='remote-gateway',
                        allowed_cidr='192.0.2.1/32',inference=inference)
                    aws=SimpleNamespace(apply=False)
                    output=io.StringIO()
                    with self.subTest(inference=inference,public=public),patch.object(vm,'plan',return_value={'Plan':'test'}),patch.object(vm,'apply_plan') as apply,patch('builtins.input') as prompt,contextlib.redirect_stdout(output):
                        vm.execute_plan(aws,args)
                        apply.assert_not_called()
                        prompt.assert_not_called()
                    self.assertFalse(aws.apply)
                    self.assertIn('Read-only plan complete',output.getvalue())

    def test_new_launch_only_applies_after_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=SimpleNamespace(state_file=Path(tmp)/'new',mode='apply',ssh_access='restricted',
                https_access='restricted',scenario='remote-gateway',allowed_cidr='192.0.2.1/32',
                inference='none',account_id='123456789012',region='eu-central-1',prefix='test-run')
            aws=SimpleNamespace(apply=False)
            proposal={'new':'plan'}
            with patch.object(vm,'plan',return_value=proposal),patch.object(vm,'apply_plan') as apply,patch('builtins.input',return_value='launch 123456789012 eu-central-1 test-run'),contextlib.redirect_stdout(io.StringIO()):
                vm.execute_plan(aws,args)
            self.assertTrue(aws.apply)
            apply.assert_called_once_with(aws,args,proposal)

    def test_retry_is_reconciled_again_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state'
            path.write_text('synthetic journal')
            args=SimpleNamespace(state_file=path,mode='apply',ssh_access='restricted',
                https_access='restricted',scenario='remote-gateway',allowed_cidr='192.0.2.1/32',
                inference='none',account_id='123456789012',region='eu-central-1',prefix='test-run')
            for recovered in (False,True):
                proposal={'Action':'recover' if recovered else 'retry'}
                if recovered:proposal['RecoveredInstanceId']='i-test'
                aws=SimpleNamespace(apply=False)
                state={'existing':'journal'}
                with self.subTest(recovered=recovered),patch.object(vm,'retry_state',return_value=state),patch.object(vm,'retry_plan',return_value=proposal) as review,patch.object(vm,'apply_retry') as apply,patch('builtins.input',return_value=('recover' if recovered else 'launch')+' 123456789012 eu-central-1 test-run'),contextlib.redirect_stdout(io.StringIO()):
                    vm.execute_plan(aws,args)
                    self.assertEqual(review.call_count,2)
                    self.assertTrue(aws.apply)
                    apply.assert_called_once_with(aws,args,state,proposal)


class JournalAndDiscovery(unittest.TestCase):
    def test_journals_reject_wrong_scope_and_unsafe_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state'
            args=SimpleNamespace(state_file=path,account_id='123456789012',region='eu-central-1')
            for value in ([],{'ApplyStatus':'complete'}, {'InstanceId':'i-existing'}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value),self.assertRaises(ValueError):vm.retry_state(args)
            path.unlink();path.symlink_to(Path(tmp)/'missing')
            with self.assertRaisesRegex(ValueError,'symlink'):vm.retry_state(args)
            path.unlink()
            state={'AccountId':args.account_id,'Region':args.region,'Scenario':'remote-gateway',
                   'Prefix':'test-run','InstanceId':'i-test','SecurityGroupId':'sg-test','Ingress':[]}
            for field,value,scenario in (('AccountId','999999999999',None),('Region','us-east-1',None),
                                          ('Scenario','remote-gateway','vllm-server')):
                path.write_text(json.dumps({**state,field:value}))
                with self.subTest(field=field),self.assertRaises(ValueError):vm.launch_state(path,args,scenario)
            aws=Mock();aws.call.return_value={'Reservations':[]}
            with self.assertRaisesRegex(ValueError,'not found'):vm.described_instance(aws,state,'remote-gateway')
            with self.assertRaisesRegex(ValueError,'ownership'):vm.check_owned({'Tags':[]},state)

    def test_endpoint_rejects_invalid_client_scope_and_server_identity(self):
        args=SimpleNamespace(account_id='123456789012',client_state_file=Path('/tmp/client'),client_vpc_id='vpc-test')
        aws=Mock()
        for case,message in (('role','non-vLLM'),('no-vpc','VPC is required'),('vpc-drift','selected client VPC'),
                             ('tags','ownership/scenario'),('no-address','no private IPv4')):
            client={'Scenario':'vllm-server' if case=='role' else 'remote-gateway'}
            args.client_vpc_id=None if case=='no-vpc' else 'vpc-test'
            server={'Tags':[] if case=='tags' else vm.tags('test-vllm','vllm-server')}
            with self.subTest(case=case),patch.object(vm,'check_identity'),patch.object(vm,'launch_state',return_value=client),patch.object(vm,'described_instance',return_value={'VpcId':'foreign' if case=='vpc-drift' else 'vpc-test'}),patch.object(vm,'vllm_instances',return_value=[server]),self.assertRaisesRegex(ValueError,message):
                vm.vllm_endpoint(aws,args)
        state={'InstanceId':'i-test','Prefix':'test-run','SecurityGroupId':'sg-test'}
        aws.call.return_value={'Reservations':[{'Instances':[{'State':{'Name':'running'},
            'Tags':vm.tags('test-run','remote-gateway'),'SecurityGroups':[{'GroupId':'sg-other'}]}]}]}
        with self.assertRaisesRegex(ValueError,'attached security'):vm.described_instance(aws,state,'remote-gateway')

    def test_invalid_scenario_and_grant_journal_idempotency(self):
        args=SimpleNamespace(**dict.fromkeys(vm.CONFIG_KEYS | {'config'}))
        args.scenario='unsupported'
        with self.assertRaisesRegex(ValueError,'supported gateway'):vm.configure(args)
        state={'ManagedGrants':['sg-client']}
        with patch.object(vm,'update_state') as update:
            vm.record_vllm_grant(state,SimpleNamespace(vllm_state_file=Path('/tmp/state')),'sg-client')
            update.assert_not_called()
        with self.assertRaisesRegex(ValueError,'managed vLLM grants differ'):
            vm.check_ingress({'IpPermissions':[]},{'Ingress':[],'ManagedGrants':['sg-missing'],'Scenario':'vllm-server'})

    def test_vllm_selection_uses_exact_instance_id_or_full_prefix(self):
        aws=Mock();aws.call.return_value={'Reservations':[{'Instances':[{'InstanceId':'i-123abc'}]}]}
        args=SimpleNamespace(vllm_instance_id='i-123abc',vllm_prefix=None)
        self.assertEqual(vm.vllm_instances(aws,args),[{'InstanceId':'i-123abc'}])
        self.assertEqual(aws.call.call_args.kwargs['instance_ids'],['i-123abc'])
        for identifier,prefix in (('i-123abc','some-prefix'),('bad',None),(None,None),(None,'*')):
            aws.call.reset_mock()
            args.vllm_instance_id=identifier;args.vllm_prefix=prefix
            with self.subTest(identifier=identifier,prefix=prefix),self.assertRaises(ValueError):vm.vllm_instances(aws,args)
            aws.call.assert_not_called()
        # Rules for unrelated protocols do not grant TCP inference access.
        with self.assertRaisesRegex(ValueError,'no source-group'):
            vm.check_vllm_ingress_is_source_group_only([{'IpPermissions':[{'IpProtocol':'udp','FromPort':8000,'ToPort':8000}]}],'sg-client')

    def test_auxiliary_hosts_forbid_public_ssh_and_inference(self):
        for field,value in (('ssh_access','public'),('inference','cpu')):
            args=SimpleNamespace(**dict.fromkeys(vm.CONFIG_KEYS | {'config'}))
            args.scenario='keycloak'
            setattr(args,field,value)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'auxiliary test hosts'):
                vm.configure(args)
        for field,value in (('Ipv6Ranges',[{'CidrIpv6':'::/0'}]),('PrefixListIds',[{'PrefixListId':'pl-test'}])):
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'non-IPv4'):
                vm.check_ingress({'IpPermissions':[{'IpProtocol':'tcp','FromPort':22,'ToPort':22,field:value}]},
                                 {'Ingress':[]})

    def test_launch_denial_is_not_reclassified_as_capacity_or_success(self):
        args=SimpleNamespace(prefix='test-run',scenario='remote-gateway',subnet_id='subnet-test',volume_gib=50)
        proposal={'ImageId':'ami-test','InstanceType':'m7i.2xlarge','RootDeviceName':'/dev/sda1'}
        state={'ClientToken':'test-token','KeyPairName':'test-run','SecurityGroupId':'sg-test','ApplyStatus':'launch-requested'}
        aws=Mock();aws.call.side_effect=vm.AwsError('UnauthorizedOperation','denied')
        with patch.object(vm,'update_state') as update,self.assertRaisesRegex(ValueError,'denied'):
            vm.launch_instance(aws,args,proposal,state)
        update.assert_not_called()
        self.assertEqual(state['ApplyStatus'],'launch-requested')
        self.assertNotIn('InstanceId',state)

    def test_foreign_capacity_subnet_and_private_public_key_fail_before_further_reads(self):
        aws=Mock()
        args=SimpleNamespace(account_id='123456789012',subnet_id='subnet-test',instance_type='m7i.2xlarge')
        aws.call.side_effect=[{'Account':args.account_id,'Arn':'arn:aws:iam::123456789012:user/test'},
                              {'Subnets':[{'OwnerId':'999999999999'}]}]
        with self.assertRaisesRegex(ValueError,'expected account'):vm.capacity_report(aws,args)
        self.assertEqual(aws.call.call_count,2)
        with tempfile.TemporaryDirectory() as tmp:
            args.public_key=Path(tmp)/'key'
            for key in ('-----BEGIN PRIVATE KEY-----','ssh-ed25519 one\nssh-ed25519 two','ssh-dss synthetic'):
                args.public_key.write_text(key)
                aws.call.reset_mock()
                with self.subTest(key=key),patch.object(vm.subprocess,'run'),self.assertRaisesRegex(ValueError,'public key'):
                    vm.plan(aws,args)
                aws.call.assert_not_called()


class GrantRaces(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.client={'Scenario':'remote-gateway','Prefix':'test-client','SecurityGroupId':'sg-client'}
        self.server={'Scenario':'vllm-server','Prefix':'test-vllm','SecurityGroupId':'sg-vllm'}
        self.args=SimpleNamespace(account_id='123456789012',region='eu-central-1',
            client_state_file=Path('/tmp/client'),vllm_state_file=Path('/tmp/vllm'),apply=True)
        self.aws=Mock()
        self.aws.call.return_value={'SecurityGroups':[{}]}
        self.stack.enter_context(patch.object(vm,'check_identity'))
        self.stack.enter_context(patch.object(vm,'launch_state',side_effect=[self.client,self.server]))
        self.stack.enter_context(patch.object(vm,'described_instance',side_effect=[
            {'InstanceId':'i-client','VpcId':'vpc-test'}, {'InstanceId':'i-vllm','VpcId':'vpc-test'}]))
        self.stack.enter_context(patch.object(vm,'owned_tags',return_value=True))
        self.stack.enter_context(patch.object(vm,'check_ingress'))
        self.record=self.stack.enter_context(patch.object(vm,'record_vllm_grant'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_existing_grant_readonly_does_not_update_journal(self):
        self.args.apply=False
        with patch.object(vm,'validate_vllm_grant_state',return_value=({},True)):
            vm.vllm_grant(self.aws,self.args)
        self.record.assert_not_called()

    def test_foreign_vpc_stops_before_grant(self):
        with patch.object(vm,'described_instance',side_effect=[{'VpcId':'vpc-a'},{'VpcId':'vpc-b'}]):
            with self.assertRaisesRegex(ValueError,'same VPC'):vm.vllm_grant(self.aws,self.args)
        self.record.assert_not_called()

    def test_cancel_never_records_or_writes(self):
        with patch.object(vm,'validate_vllm_grant_state',return_value=({},False)),patch('builtins.input',return_value='cancel'):
            with self.assertRaisesRegex(ValueError,'confirmation'):vm.vllm_grant(self.aws,self.args)
        self.record.assert_not_called()
        self.assertEqual(self.aws.call.call_count,1)

    def test_concurrent_grant_after_confirmation_does_not_write_again(self):
        with patch.object(vm,'validate_vllm_grant_state',side_effect=[({},False),({},True)]),patch('builtins.input',return_value='grant 123456789012 eu-central-1 sg-vllm sg-client'):
            vm.vllm_grant(self.aws,self.args)
        self.record.assert_called_once()
        self.assertEqual(self.aws.call.call_count,1)

    def test_permission_denial_does_not_record_success(self):
        self.aws.call.side_effect=[{'SecurityGroups':[{}]},vm.AwsError('AccessDenied','denied')]
        with patch.object(vm,'validate_vllm_grant_state',return_value=({},False)),patch('builtins.input',return_value='grant 123456789012 eu-central-1 sg-vllm sg-client'):
            with self.assertRaisesRegex(ValueError,'denied'):vm.vllm_grant(self.aws,self.args)
        self.record.assert_not_called()

    def test_duplicate_without_observed_grant_does_not_record_success(self):
        self.aws.call.side_effect=[{'SecurityGroups':[{}]},vm.AwsError('InvalidPermission.Duplicate','duplicate')]
        with patch.object(vm,'validate_vllm_grant_state',return_value=({},False)),patch('builtins.input',return_value='grant 123456789012 eu-central-1 sg-vllm sg-client'):
            with self.assertRaisesRegex(ValueError,'not visible'):vm.vllm_grant(self.aws,self.args)
        self.record.assert_not_called()


if __name__ == '__main__':
    unittest.main()
