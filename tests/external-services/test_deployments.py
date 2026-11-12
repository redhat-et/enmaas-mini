"""Deployment inventory and DNS scope must not accept secrets or broad permissions."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest

spec = importlib.util.spec_from_file_location('deployments', Path(__file__).with_name('deployments.py'))
deployments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployments)


class Deployments(unittest.TestCase):
    def test_trust_policy_requires_exact_same_account_principal_and_user_mfa(self):
        value=self.sample()
        arn='arn:aws:iam::123456789012:user/operators/test-user'
        statement=deployments.trust_policy(value,arn)['Statement'][0]
        self.assertEqual(statement['Principal'],{'AWS':arn})
        self.assertEqual(statement['Action'],'sts:AssumeRole')
        self.assertEqual(statement['Condition'],{'Bool':{'aws:MultiFactorAuthPresent':'true'}})
        for invalid in ('*','arn:aws:iam::123456789012:root',
                        'arn:aws:iam::999999999999:user/test-user',
                        'arn:aws:iam::123456789012:user/operators/*',
                        'arn:aws:sts::123456789012:assumed-role/admin/session'):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                deployments.trust_policy(value,invalid)

    def test_role_trust_requires_explicit_choice_and_does_not_claim_mfa(self):
        value=self.sample();arn='arn:aws:iam::123456789012:role/operators/federated'
        with self.assertRaises(ValueError):deployments.trust_policy(value,arn)
        statement=deployments.trust_policy(value,arn,allow_role=True)['Statement'][0]
        self.assertEqual(statement['Principal'],{'AWS':arn})
        self.assertNotIn('Condition',statement)
        self.assertNotIn('*',str(statement))

    def test_group_runs_share_profile_but_not_resource_identity(self):
        first = deployments.new('123456789012', 'eu-central-1', '/tmp/key', 'team-role', group='demo')
        second = deployments.new('123456789012', 'eu-central-1', '/tmp/key', 'team-role', group='demo')
        self.assertEqual(first['version'], 2)
        self.assertEqual(first['group'], second['group'])
        self.assertEqual(first['aws_profile'], second['aws_profile'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertRegex(first['id'], r'^em-demo-[a-f0-9]{12}$')
        self.assertIn('DEPLOYMENT_GROUP=demo', deployments.shell_selection(first))
        for invalid in ('', 'toolong99', 'a-b', 'UPPER', '*', '1demo'):
            with self.subTest(group=invalid), self.assertRaises(ValueError):
                deployments.new('123456789012', 'eu-central-1', '/tmp/key', 'team-role', group=invalid)
        first['group'] = 'other'
        with self.assertRaises(ValueError):
            deployments.validate(first)

    def test_legacy_manifest_does_not_adopt_group(self):
        value = self.sample()
        self.assertEqual(value['version'], 1)
        self.assertNotIn('group', value)
        self.assertIn('unset DEPLOYMENT_GROUP', deployments.shell_selection(value))
        value['group'] = 'demo'
        with self.assertRaises(ValueError):
            deployments.validate(value)

    def test_summary_hides_unlisted_fields_and_rejects_wrong_run(self):
        value = deployments.new('123456789012', 'eu-central-1', '/tmp/key', 'team-role', group='demo')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {'AccountId': value['account'], 'Region': value['region'],
                     'Prefix': value['id']+'-gateway', 'Scenario': 'remote-gateway',
                     'InstanceId': 'i-test', 'ApplyStatus': 'launch-requested',
                     'UnexpectedCredential': 'must-not-print'}
            path = root / (value['id']+'-gateway.json')
            path.write_text(json.dumps(state)); path.chmod(0o600)
            report = deployments.summary(value, root)
            self.assertNotIn('must-not-print', json.dumps(report))
            self.assertEqual(report['vms'][0]['InstanceId'], 'i-test')
            self.assertEqual(report['vms'][1]['ApplyStatus'], 'No launch journal')
            state['Prefix'] = 'another-run-gateway'
            path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                deployments.summary(value, root)

    def sample(self):
        return deployments.new('123456789012', 'eu-central-1', '/tmp/test-key',
                               'test-profile', 'Z123ABC', 'lab.example.com')

    def test_unique_private_inventory_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'deployments'
            first, second = self.sample(), self.sample()
            self.assertNotEqual(first['id'], second['id'])
            path = deployments.save(root, first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(deployments.load(path), first)
            with self.assertRaises(FileExistsError):
                deployments.save(root, first)

    def test_no_secret_or_unknown_field_accepted(self):
        for field in ('secret', 'AWS_SECRET_ACCESS_KEY', 'provider_key', 'resources'):
            value = self.sample()
            value[field] = 'do-not-store'
            with self.assertRaises(ValueError):
                deployments.validate(value)

    def test_scope_rejects_unsafe_inputs(self):
        for zone, domain in [('Z123', '*.example.com'), ('Z123', 'example.com/evil'),
                             ('*', 'example.com'), ('Z123', ''), ('', 'example.com')]:
            with self.assertRaises(ValueError):
                deployments.new('123456789012', 'eu-central-1', '/tmp/key', 'lab', zone, domain)

    def test_dns_policy_only_exact_deployment_names(self):
        value = self.sample()
        policy = deployments.dns_policy(value)
        statement = policy['Statement'][0]
        self.assertEqual(statement['Resource'], 'arn:aws:route53:::hostedzone/Z123ABC')
        conditions = statement['Condition']['ForAllValues:StringEquals']
        self.assertEqual(conditions['route53:ChangeResourceRecordSetsActions'], ['CREATE', 'DELETE'])
        self.assertEqual(conditions['route53:ChangeResourceRecordSetsRecordTypes'], ['A', 'TXT'])
        names = conditions['route53:ChangeResourceRecordSetsNormalizedRecordNames']
        self.assertEqual(len(names), 4)
        self.assertTrue(all(value['id'] in name for name in names))
        self.assertNotIn('*', json.dumps(policy))
        self.assertNotIn('DeleteHostedZone', json.dumps(policy))

    def test_rejects_symlink_or_public_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = deployments.save(Path(tmp) / 'registry', self.sample())
            link = Path(tmp) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                deployments.load(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                deployments.load(path)

    def test_shell_selection_quotes_paths_and_contains_no_credentials(self):
        value = self.sample()
        value['ssh_key_path'] = "/tmp/key with spaces/$(printf injected)`printf injected`'key"
        rendered = deployments.shell_selection(value)
        result = subprocess.run(['bash', '-c', rendered+'\nprintf "%s" "$SSH_KEY"'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, value['ssh_key_path'])
        self.assertIn('AWS_TEST_EXPECTED_ACCOUNT=', rendered)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY=', rendered)

    def test_selection_clears_previous_credentials_and_host_without_printing_them(self):
        value = self.sample()
        stale = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                 'AWS_SECURITY_TOKEN', 'AWS_DEFAULT_PROFILE', 'AWS_TEST_READY',
                 'AWS_TEST_PLAN_INPUTS', 'AWS_TEST_CREDENTIALS', 'ACCOUNT',
                 'RHEL_HOST', 'RHEL_SCENARIO', 'RHEL_INFERENCE', 'RHEL_VPC_ID', 'RHEL_STATE_FILE')
        environment = {**os.environ, **{key: 'stale-value-do-not-print' for key in stale}}
        rendered = deployments.shell_selection(value)
        checks = '\n'.join('test -z "${'+key+'+set}" || exit 1' for key in stale)
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, '-c', rendered+'\n'+checks+
                                         '\nprintf "%s" "$AWS_PROFILE"'],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, value['aws_profile'])
                self.assertNotIn('stale-value-do-not-print', result.stdout+result.stderr+rendered)

    def test_ip_only_profile_has_no_dns_permissions(self):
        value = deployments.new('123456789012', 'eu-central-1', '/tmp/key', 'lab')
        self.assertEqual(deployments.dns_policy(value)['Statement'], [])

    def test_inspection_policy_has_no_mutation_or_secret_permissions(self):
        value=self.sample()
        statements=deployments.inspection_policy(value)['Statement']
        for statement in statements:
            actions=statement['Action']
            self.assertTrue(all('*' not in action for action in actions))
            self.assertTrue(all(action.split(':')[1].startswith(('Describe','List','GetHostedZone','GetCallerIdentity')) for action in actions))
            self.assertTrue(all(action.split(':')[0] in ('ec2','rds','sts','route53') for action in actions))
            if any(action.startswith(('ec2:','rds:')) for action in actions):
                self.assertEqual(statement['Condition']['StringEquals']['aws:RequestedRegion'],value['region'])
        tags=next(s for s in statements if 'rds:ListTagsForResource' in s['Action'])
        self.assertTrue(all(value['account'] in arn and value['id']+'-postgres' in arn and '*' not in arn for arn in tags['Resource']))
        dns=next(s for s in statements if 'route53:GetHostedZone' in s['Action'])
        self.assertEqual(dns['Resource'],'arn:aws:route53:::hostedzone/'+value['zone_id'])
        value['zone_id']=value['domain']=''
        self.assertFalse(any(action.startswith('route53:') for s in deployments.inspection_policy(value)['Statement'] for action in s['Action']))


if __name__ == '__main__':
    unittest.main()
