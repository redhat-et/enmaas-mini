#!/usr/bin/env python3
"""Peer networking never grants public backends or trusts stale instance journals."""
import importlib.util
from pathlib import Path
import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import contextlib
import io
import sys
import os
import subprocess

spec=importlib.util.spec_from_file_location('network',(Path(__file__).resolve().parents[1] / 'network.py'))
network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)

class Network(unittest.TestCase):
    def hosts(self):
        return {role:{'VpcId':'vpc-test','PublicIpAddress':f'198.51.100.{index}',
                      'PrivateIpAddress':f'10.0.0.{index}',
                      'SecurityGroups':[{'GroupId':f'sg-{role}'}]}
                for index,role in enumerate(network.ROLES,10)}

    def test_exact_peer_ports_and_sources(self):
        plan=network.peer_rules(self.hosts())
        self.assertEqual(len(plan),3)
        issuer=[r for r in plan if r['target']=='keycloak']
        self.assertEqual({r['permission']['IpRanges'][0]['CidrIp'] for r in issuer},
                         {'198.51.100.10/32','198.51.100.12/32'})
        self.assertTrue(all(r['permission']['FromPort']==8443 for r in issuer))
        collector=[r for r in plan if r['target']=='observability'][0]['permission']
        self.assertEqual(collector['FromPort'],24443)
        self.assertEqual(collector['UserIdGroupPairs'],[{'GroupId':'sg-gateway'}])
        self.assertNotIn('IpRanges',collector)

    def test_changed_vpc_missing_or_duplicate_addresses_rejected(self):
        for change in ('vpc','missing','duplicate'):
            hosts=self.hosts()
            if change=='vpc':hosts['keycloak']['VpcId']='vpc-other'
            if change=='missing':del hosts['gateway']['PublicIpAddress']
            if change=='duplicate':hosts['keycloak']['PublicIpAddress']=hosts['gateway']['PublicIpAddress']
            with self.assertRaises(ValueError):network.peer_rules(hosts)

    def test_private_issuer_uses_only_exact_peer_security_groups(self):
        plan=network.peer_rules(self.hosts(),issuer_path='private')
        issuer=[r['permission'] for r in plan if r['target']=='keycloak']
        self.assertEqual({r['UserIdGroupPairs'][0]['GroupId'] for r in issuer},
                         {'sg-gateway','sg-observability'})
        self.assertTrue(all(r['FromPort']==8443 and r['ToPort']==8443 for r in issuer))
        self.assertTrue(all('IpRanges' not in r for r in issuer))
        for address in ('198.51.100.10','127.0.0.1','169.254.1.1','0.0.0.0'):
            hosts=self.hosts();hosts['gateway']['PrivateIpAddress']=address
            with self.subTest(address=address),self.assertRaises(ValueError):
                network.peer_rules(hosts,issuer_path='private')
        with self.assertRaises(ValueError):network.peer_rules(self.hosts(),issuer_path='unknown')

    def test_normalization_ignores_description_not_broader_access(self):
        rule={'IpProtocol':'tcp','FromPort':24443,'ToPort':24443,
              'UserIdGroupPairs':[{'GroupId':'sg-gateway','Description':'lab','UserId':'account'}]}
        expected={('tcp',24443,24443,'group','sg-gateway')}
        self.assertEqual(network.ruleset([rule]),expected)
        for field,value in [('IpProtocol','-1'),('ToPort',65535)]:
            changed=dict(rule);changed[field]=value
            self.assertNotEqual(network.ruleset([changed]),expected)
        self.assertNotEqual(network.ruleset([{'IpProtocol':'tcp','FromPort':24443,'ToPort':24443,
                            'Ipv6Ranges':[{'CidrIpv6':'::/0'}]}]),expected)
        self.assertEqual(network.ruleset([{'IpProtocol':'-1'}]), {('-1',None,None,'unknown','')})
        self.assertNotEqual(network.ruleset([{'IpProtocol':'tcp','FromPort':24443,'ToPort':24443,
                            'PrefixListIds':[{'PrefixListId':'pl-test'}]}]),expected)

    def test_wrong_caller_account_fails_before_resource_reads(self):
        aws=Mock()
        aws.call.return_value={'Account':'999999999999'}
        with self.assertRaisesRegex(ValueError,'caller account'):
            network.inspect(aws,SimpleNamespace(account_id='123456789012'))
        aws.call.assert_called_once_with('sts','get-caller-identity')

    def test_inspection_checks_account_ownership_and_actual_ingress(self):
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(account_id='123456789012',region='eu-test-1',prefix='lab',state_dir=Path(directory))
            hosts=self.hosts();groups={}
            baseline=[{'IpProtocol':'tcp','FromPort':p,'ToPort':p,
                       'IpRanges':[{'CidrIp':'192.0.2.1/32'}]} for p in (22,8443)]
            for role,host in hosts.items():
                scenario='remote-gateway' if role=='gateway' else role
                tags=network.vm.tags('lab-'+role,scenario)
                host.update(InstanceId='i-'+role,State={'Name':'running'},Tags=tags)
                groups['sg-'+role]={'OwnerId':args.account_id,'VpcId':host['VpcId'],
                    'Tags':tags,'IpPermissions':list(baseline)}
                (args.state_dir/('lab-'+role+'.json')).write_text(json.dumps({
                    'AccountId':args.account_id,'Region':args.region,'Prefix':'lab-'+role,
                    'Scenario':scenario,'InstanceId':'i-'+role,'SecurityGroupId':'sg-'+role,'Ingress':baseline}))
            def call(service,action,**options):
                if action=='get-caller-identity':return {'Account':args.account_id}
                if action=='describe-instances':
                    role=options['instance_ids'][0][2:]
                    return {'Reservations':[{'Instances':[hosts[role]]}]}
                if action=='describe-security-groups':return {'SecurityGroups':[groups[options['group_ids'][0]]]}
                self.fail('Unexpected AWS write or read: '+action)
            aws=Mock();aws.call.side_effect=call
            with patch.object(network.vm,'launch_state',return_value={'Prefix':'another-run'}):
                with self.assertRaisesRegex(ValueError,'Journal run prefix'):
                    network.inspect(aws,args)
            original=groups['sg-gateway']
            def missing_group(service,action,**options):
                if action=='describe-security-groups':return {'SecurityGroups':[]}
                return call(service,action,**options)
            aws.call.side_effect=missing_group
            with self.assertRaisesRegex(ValueError,'Security group missing'):
                network.inspect(aws,args)
            aws.call.side_effect=call
            original['IpPermissions']=baseline[:1]
            with self.assertRaisesRegex(ValueError,'Original restricted ingress is missing'):
                network.inspect(aws,args)
            original['IpPermissions']=list(baseline)
            journal=args.state_dir/'lab-gateway.json'
            content=journal.read_text()
            altered=json.loads(content)
            altered['Ingress']=[{'IpProtocol':'tcp','FromPort':22,'ToPort':22,
                                 'IpRanges':[{'CidrIp':'0.0.0.0/0'}]}]
            journal.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError,'restricted workstation'):
                network.inspect(aws,args)
            journal.write_text(content)
            _,missing=network.inspect(aws,args)
            self.assertEqual(len(missing),3)
            for rule in missing:groups[rule['group_id']]['IpPermissions'].append(rule['permission'])
            self.assertEqual(network.inspect(aws,args)[1],[])
            args.issuer_path='private'
            with self.assertRaisesRegex(ValueError,'Unexpected ingress'):
                network.inspect(aws,args)  # Never silently retain the public-IP peer path.
            groups['sg-keycloak']['IpPermissions']=list(baseline)
            _,private_missing=network.inspect(aws,args)
            self.assertEqual(len(private_missing),2)
            for rule in private_missing:
                self.assertIn('UserIdGroupPairs',rule['permission'])
                groups[rule['group_id']]['IpPermissions'].append(rule['permission'])
            self.assertEqual(network.inspect(aws,args)[1],[])
            groups['sg-keycloak']['IpPermissions'].append({'IpProtocol':'-1','IpRanges':[{'CidrIp':'0.0.0.0/0'}]})
            with self.assertRaisesRegex(ValueError,'Unexpected ingress'):network.inspect(aws,args)
            groups['sg-keycloak']['IpPermissions'].pop()
            groups['sg-gateway']['OwnerId']='999999999999'
            with self.assertRaisesRegex(ValueError,'ownership'):network.inspect(aws,args)
            groups['sg-gateway']['OwnerId']=args.account_id
            hosts['gateway']['State']['Name']='stopped'
            with self.assertRaisesRegex(ValueError,'not running'):network.inspect(aws,args)

class NetworkCLI(unittest.TestCase):
    def test_cli_wrong_account_reports_failure_without_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli=Path(tmp)/'aws'
            cli.write_text('#!/bin/sh\nprintf \'{"Account":"999999999999"}\\n\'\n')
            cli.chmod(0o700)
            result=subprocess.run([sys.executable,str((Path(__file__).resolve().parents[1] / 'network.py')),
                '--account-id','123456789012','--region','eu-test-1','--prefix','lab'],
                env={**os.environ,'PATH':tmp+os.pathsep+os.environ['PATH']},capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Network setup stopped: AWS caller account differs',result.stderr)

    def setUp(self):
        self.argv = ['network.py', '--account-id', '123456789012', '--region', 'eu-test-1', '--prefix', 'lab']
        self.hosts = Network().hosts()
        self.rules = [{**rule, 'group_id': 'sg-'+rule['target']} for rule in network.peer_rules(self.hosts)]
        self.confirmation = 'grant peers 123456789012 eu-test-1 lab'

    def invoke(self, snapshots, apply=True, answer=None, error=None):
        aws = Mock()
        aws.call.side_effect = error
        output = io.StringIO()
        with patch.object(sys, 'argv', self.argv+(['--apply'] if apply else [])), patch.object(
                network.vm, 'Aws', return_value=aws), patch.object(network, 'inspect', side_effect=snapshots) as inspect, patch(
                'builtins.input', return_value=self.confirmation if answer is None else answer) as prompt, contextlib.redirect_stdout(output):
            network.main()
        return aws, inspect, prompt, output.getvalue()

    def test_plan_and_already_applied_never_prompt_or_write(self):
        for apply, rules in ((False, self.rules), (True, [])):
            with self.subTest(apply=apply):
                aws, inspect, prompt, output = self.invoke([(self.hosts, rules)], apply=apply)
                aws.call.assert_not_called()
                prompt.assert_not_called()
                self.assertEqual(inspect.call_count, 1)
                self.assertIn('TelemetryAddress', output)

    def test_headless_confirmation_is_exact_and_never_prompts(self):
        for answer in (self.confirmation,'wrong',''):
            aws=Mock()
            with patch.object(sys,'argv',self.argv+['--apply','--confirm',answer]),patch.object(network.vm,'Aws',return_value=aws), \
                 patch.object(network,'inspect',side_effect=[(self.hosts,self.rules),(self.hosts,self.rules),(self.hosts,[])]), \
                 patch('builtins.input') as prompt,contextlib.redirect_stdout(io.StringIO()):
                if answer==self.confirmation:network.main()
                else:
                    with self.assertRaisesRegex(ValueError,'Confirmation'):network.main()
                    aws.call.assert_not_called()
                prompt.assert_not_called()
        with patch.object(sys,'argv',self.argv+['--confirm',self.confirmation]),patch.object(network.vm,'Aws') as aws, \
             contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            network.main()
        aws.assert_not_called()

    def test_apply_rechecks_and_adds_only_reviewed_rules(self):
        aws, inspect, prompt, output = self.invoke([(self.hosts, self.rules), (self.hosts, self.rules), (self.hosts, [])])
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual(aws.call.call_count, 3)
        for call, rule in zip(aws.call.call_args_list, self.rules):
            self.assertEqual(call.args, ('ec2', 'authorize-security-group-ingress'))
            self.assertEqual(call.kwargs, {'group_id': rule['group_id'], 'ip_permissions': [rule['permission']]})
        self.assertIn('Peer ingress verified', output)
        prompt.assert_called_once()

    def test_cancel_or_resource_drift_never_authorizes(self):
        changed_hosts = dict(self.hosts)
        changed_hosts['gateway'] = dict(changed_hosts['gateway'], PrivateIpAddress='10.0.0.99')
        cases = [([(self.hosts, self.rules)], 'no', 'Confirmation'),
                 ([(self.hosts, self.rules), (changed_hosts, self.rules)], None, 'Resources changed'),
                 ([(self.hosts, self.rules), (self.hosts, self.rules[:-1])], None, 'Resources changed')]
        for snapshots, answer, message in cases:
            aws = Mock()
            with self.subTest(message=message), patch.object(sys, 'argv', self.argv+['--apply']), patch.object(network.vm, 'Aws', return_value=aws), patch.object(network, 'inspect', side_effect=snapshots), patch('builtins.input', return_value=answer or self.confirmation), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, message):
                    network.main()
                aws.call.assert_not_called()

    def test_duplicate_is_reverified_but_other_write_errors_stop(self):
        snapshots = [(self.hosts, self.rules), (self.hosts, self.rules), (self.hosts, [])]
        aws, inspect, _, output = self.invoke(snapshots, error=network.vm.AwsError('InvalidPermission.Duplicate', 'already present'))
        self.assertEqual(aws.call.call_count, 3)
        self.assertEqual(inspect.call_count, 3)
        self.assertIn('verified', output)
        with self.assertRaisesRegex(ValueError, 'denied'):
            self.invoke(snapshots, error=network.vm.AwsError('UnauthorizedOperation', 'denied'))
        with self.assertRaisesRegex(ValueError, 'Some peer rules are missing'):
            self.invoke([(self.hosts, self.rules)]*3)

    def test_invalid_account_and_prefix_never_construct_client(self):
        for option, value in (('--account-id', 'wrong'), ('--prefix', 'bad/name')):
            argv = self.argv.copy()
            argv[argv.index(option)+1] = value
            with self.subTest(option=option), patch.object(sys, 'argv', argv), patch.object(network.vm, 'Aws') as client, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    network.main()
                client.assert_not_called()


if __name__=='__main__':unittest.main()
