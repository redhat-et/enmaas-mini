#!/usr/bin/env python3
"""Peer networking never grants public backends or trusts stale instance journals."""
import importlib.util
from pathlib import Path
import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('network',Path(__file__).with_name('network.py'))
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

if __name__=='__main__':unittest.main()
