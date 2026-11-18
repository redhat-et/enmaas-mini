"""Host handoff rejects foreign journals, pending VMs and broadened ingress."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('infra_info',(Path(__file__).resolve().parents[1] / 'infrastructure-info.py'))
info=importlib.util.module_from_spec(spec);spec.loader.exec_module(info)


class Summary(unittest.TestCase):
    def fixture(self,root):
        m=info.registry.new('123456789012','eu-central-1','/tmp/key','operator',group='lab')
        hosts={};states={};groups={}
        for index,role in enumerate(info.peers.ROLES):
            scenario='remote-gateway' if role=='gateway' else role
            state={'AccountId':m['account'],'Region':m['region'],'Prefix':m['id']+'-'+role,
                'Scenario':scenario,'VpcId':'vpc-12345678','InstanceId':'i-'+str(index),
                'SecurityGroupId':'sg-'+str(index),'Ingress':info.peers.vm.ingress(scenario,'192.0.2.1/32')}
            states[role]=state
            path=root/(m['id']+'-'+role+'.json');path.write_text(json.dumps(state));path.chmod(0o600)
            tags=info.peers.vm.tags(state['Prefix'],scenario)
            hosts[role]={'InstanceId':state['InstanceId'],'State':{'Name':'running'},'Tags':tags,
                'VpcId':state['VpcId'],'PublicIpAddress':'198.51.100.'+str(index+1),
                'PrivateIpAddress':'10.42.0.'+str(index+1),
                'SecurityGroups':[{'GroupId':state['SecurityGroupId']}]}
            groups[state['SecurityGroupId']]={'OwnerId':m['account'],'VpcId':state['VpcId'],
                'Tags':tags,'IpPermissions':list(state['Ingress'])}
        def call(service,action,**options):
            if action=='get-caller-identity':return {'Account':m['account']}
            if action=='describe-instances':
                return {'Reservations':[{'Instances':[h for h in hosts.values() if h['InstanceId'] in options['instance_ids']]}]}
            if action=='describe-security-groups':return {'SecurityGroups':[groups[options['group_ids'][0]]]}
            self.fail('Unexpected AWS action '+action)
        aws=Mock();aws.call.side_effect=call
        record={'state':'creation-recorded-complete','confirmed_resources':[{'action':'create-vpc','id':'vpc-12345678'}]}
        return m,aws,record,hosts,groups,states

    def test_dns_handoff_uses_only_live_addresses_and_shared_collision_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,aws,record,hosts,groups,states=self.fixture(root)
            m.update(zone_id='ZEXAMPLE',domain='lab.example.com')
            with patch.object(info.journals,'inspect',return_value=record),patch.object(info.dns,'execute',return_value={'state':'absent'}) as run:
                result=info.prepare_dns(m,root,aws,root/'dns.json',False,None)
            self.assertEqual(result,{'state':'absent'})
            args,selected=run.call_args.args
            self.assertEqual(selected,m);self.assertEqual(args.action,'plan')
            self.assertEqual(args.gateway_ip,hosts['gateway']['PublicIpAddress'])
            snapshot={'hosts':{'gateway':{'public_ip':'1.1.1.1'},'keycloak':{'public_ip':'8.8.8.8'},'observability':{'public_ip':'9.9.9.9'}}}
            with patch.object(info,'summary',side_effect=[snapshot,dict(snapshot,changed=True)]),patch.object(info.dns,'execute') as run,self.assertRaises(ValueError):
                info.prepare_dns(m,root,aws,root/'dns.json',True,'create '+m['id']+' '+m['account'])
            run.assert_not_called()
            with patch.object(info,'summary',return_value=snapshot),patch.object(info.dns,'execute',return_value={'changes_made':True}) as run:
                info.prepare_dns(m,root,aws,root/'dns.json',True,'create '+m['id']+' '+m['account'])
            self.assertEqual(run.call_args.args[0].action,'create')

    def test_peer_handoff_rechecks_ownership_and_only_grants_private_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,aws,record,hosts,groups,states=self.fixture(root)
            reader=aws.call.side_effect
            def call(service,action,**kwargs):
                if action=='authorize-security-group-ingress':
                    permission=kwargs['ip_permissions'][0]
                    self.assertIn('UserIdGroupPairs',permission)
                    self.assertNotIn('IpRanges',permission)
                    groups[kwargs['group_id']]['IpPermissions'].append(permission)
                    return {}
                return reader(service,action,**kwargs)
            aws.call.side_effect=call
            with patch.object(info.journals,'inspect',return_value=record):
                result=info.configure_peers(m,root,aws)
                repeated=info.configure_peers(m,root,aws)
            self.assertEqual(result['missing_private_peer_rules'],[])
            self.assertEqual(repeated,result)
            writes=[c for c in aws.call.call_args_list if c.args[1]=='authorize-security-group-ingress']
            self.assertEqual(len(writes),3)
            self.assertEqual({c.kwargs['ip_permissions'][0]['FromPort'] for c in writes},{8443,24443})

    def test_peer_handoff_stops_on_drift_and_uncertain_write(self):
        m=info.registry.new('123456789012','eu-central-1','/tmp/key','operator',group='lab')
        snapshot={'hosts':{'gateway':{'public_ip':'192.0.2.1'}},'vpc':'vpc-test',
                  'missing_private_peer_rules':[{'group_id':'sg-test','permission':{'test':True}}]}
        changed=dict(snapshot,vpc='vpc-other')
        for sequence in ([snapshot,changed],[snapshot,snapshot,snapshot],
                         [snapshot,snapshot,dict(changed,missing_private_peer_rules=[])]):
            aws=Mock()
            with patch.object(info,'summary',side_effect=sequence),self.assertRaises(ValueError):
                info.configure_peers(m,Path('/tmp/journal'),aws)
        duplicate=info.peers.vm.AwsError('InvalidPermission.Duplicate','duplicate')
        aws=Mock();aws.call.side_effect=duplicate
        done=dict(snapshot,missing_private_peer_rules=[])
        with patch.object(info,'summary',side_effect=[snapshot,snapshot,done]):
            self.assertEqual(info.configure_peers(m,Path('/tmp/journal'),aws),done)
        aws.call.side_effect=info.peers.vm.AwsError('UnauthorizedOperation','denied')
        with patch.object(info,'summary',side_effect=[snapshot,snapshot]),self.assertRaises(info.peers.vm.AwsError):
            info.configure_peers(m,Path('/tmp/journal'),aws)

    def test_actual_peer_reader_accepts_combined_filenames_without_secret_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,aws,record,hosts,groups,states=self.fixture(root)
            with patch.object(info.journals,'inspect',return_value=record):
                result=info.summary(m,root,aws)
            self.assertEqual(len(result['hosts']),3)
            self.assertEqual(len(result['missing_private_peer_rules']),3)
            self.assertFalse(result['services_verified'])
            self.assertFalse(result['ssh_host_keys_verified'])
            self.assertEqual(result['hosts']['gateway']['ssh_login'],'ec2-user@198.51.100.1')
            self.assertNotIn('ClientToken',json.dumps(result))
            self.assertTrue(all(call.args[1].startswith(('describe-','get-')) for call in aws.call.call_args_list))

    def test_incomplete_foreign_and_unsafe_states_fail_closed(self):
        for bad in ('incomplete','no-vpc','journal-vpc','live-vpc','pending','ingress','public-file'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);m,aws,record,hosts,groups,states=self.fixture(root)
                if bad=='incomplete':record['state']='uncertain-operation'
                if bad=='no-vpc':record['confirmed_resources']=[]
                if bad=='journal-vpc':
                    path=root/(m['id']+'-gateway.json');states['gateway']['VpcId']='vpc-other';path.write_text(json.dumps(states['gateway']))
                if bad=='live-vpc':
                    for h in hosts.values():h['VpcId']='vpc-other'
                    for g in groups.values():g['VpcId']='vpc-other'
                if bad=='pending':hosts['gateway']['State']['Name']='pending'
                if bad=='ingress':groups['sg-0']['IpPermissions'][0]={'IpProtocol':'-1','IpRanges':[{'CidrIp':'0.0.0.0/0'}]}
                if bad=='public-file':(root/(m['id']+'-gateway.json')).chmod(0o644)
                with patch.object(info.journals,'inspect',return_value=record),self.assertRaises(ValueError):
                    info.summary(m,root,aws)

    def test_service_config_uses_owned_live_addresses_and_exact_workstation_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,aws,record,hosts,groups,states=self.fixture(root)
            for rule in info.peers.peer_rules(hosts,'private'):
                role=rule['target'];groups[states[role]['SecurityGroupId']]['IpPermissions'].append(rule['permission'])
            options=dict(issuer_name='identity.example.com',known_hosts='/tmp/known-hosts',
                         tls_directory='/tmp/tls',revision='a'*40,source_repository='https://github.com/redhat-et/enmaas-mini.git',external_database=False)
            with patch.object(info.journals,'inspect',return_value=record):
                result=info.service_configuration(m,root,aws,**options)
            self.assertEqual(result['workstation_cidr'],'192.0.2.1/32')
            self.assertEqual(result['ssh_key'],m['ssh_key_path'])
            self.assertEqual(result['repository'],'/opt/enmaas-mini/'+m['id'])
            self.assertEqual(result['hosts']['gateway'],{'ssh':'ec2-user@198.51.100.1','private_ip':'10.42.0.1'})
            self.assertNotIn('secret',json.dumps(result).lower())
            # Both an absent peer grant and incompatible workstation rules stop handoff.
            groups['sg-0']['IpPermissions']=list(states['gateway']['Ingress'])
            groups['sg-1']['IpPermissions']=list(states['keycloak']['Ingress'])
            with patch.object(info.journals,'inspect',return_value=record),self.assertRaises(ValueError):
                info.service_configuration(m,root,aws,**options)
            summary={'hosts':{role:{'ssh_login':host['PublicIpAddress'],'private_ip':host['PrivateIpAddress']} for role,host in hosts.items()},'missing_private_peer_rules':[]}
            for ingress in ([],states['keycloak']['Ingress'][:1],info.peers.vm.ingress('keycloak','192.0.2.9/32')):
                state=dict(states['keycloak'],Ingress=ingress)
                path=root/(m['id']+'-keycloak.json');path.write_text(json.dumps(state))
                with patch.object(info,'summary',return_value=summary),self.assertRaises(ValueError):
                    info.service_configuration(m,root,aws,**options)

    def test_cli_dns_preview_creation_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,*_=self.fixture(root)
            plain=info.registry.save(root,m)
            m.update(zone_id='ZEXAMPLE',domain='lab.example.com')
            manifest=root/'dns-manifest.json';manifest.write_text(json.dumps(m));manifest.chmod(0o600)
            base=[info.__file__,'--manifest',str(manifest),'--journal',str(root)]
            for extra in (['--apply-dns'],['--confirm-dns','wrong'],['--dns-journal',str(root/'dns.json'),'--apply-dns','--confirm-dns','wrong']):
                with patch('sys.argv',base+extra),patch.object(info.peers.vm,'Aws') as client,self.assertRaises(ValueError):info.main()
                client.assert_not_called()
            for apply in (False,True):
                extra=['--dns-journal',str(root/'dns.json')]+(['--apply-dns','--confirm-dns','create '+m['id']+' '+m['account']] if apply else [])
                with patch('sys.argv',base+extra),patch.object(info.peers.vm,'Aws'), \
                     patch.object(info,'summary',return_value={'services_verified':False}), \
                     patch.object(info,'prepare_dns',return_value={'changes_made':apply}) as dns,contextlib.redirect_stdout(io.StringIO()) as output:
                    info.main()
                self.assertEqual(json.loads(output.getvalue())['dns']['changes_made'],apply)
                self.assertEqual(dns.call_args.args[4],apply)
            with patch('sys.argv',[info.__file__,'--manifest',str(plain),'--journal',str(root),'--dns-journal',str(root/'dns.json')]), \
                 patch.object(info.peers.vm,'Aws') as client,self.assertRaises(ValueError):info.main()
            client.assert_not_called()

    def test_cli_peer_confirmation_precedes_client_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,*_=self.fixture(root);path=info.registry.save(root,m)
            base=[info.__file__,'--manifest',str(path),'--journal',str(root)]
            confirmation='grant peers '+m['account']+' '+m['region']+' '+m['id']
            for extra in (['--apply-peers'],['--apply-peers','--confirm','wrong'],['--confirm',confirmation]):
                with patch('sys.argv',base+extra),patch.object(info.peers.vm,'Aws') as client,self.assertRaises(ValueError):
                    info.main()
                client.assert_not_called()
            with patch('sys.argv',base+['--apply-peers','--confirm',confirmation]), \
                 patch.object(info.peers.vm,'Aws') as client,patch.object(info,'configure_peers') as apply, \
                 patch.object(info,'summary',return_value={'verified':True}),contextlib.redirect_stdout(io.StringIO()):
                info.main()
            client.assert_called_once_with(m['region'],m['aws_profile'],True)
            apply.assert_called_once_with(m,root,client.return_value)

    def test_cli_selects_read_only_client_and_handles_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,*_=self.fixture(root);path=info.registry.save(root,m)
            args=[str(Path(info.__file__)),'--manifest',str(path),'--journal',str(root)]
            with patch('sys.argv',args),patch.object(info,'summary',return_value={'ok':True}), \
                 patch.object(info.peers.vm,'Aws') as client,contextlib.redirect_stdout(io.StringIO()) as out:
                info.main()
            client.assert_called_once_with(m['region'],m['aws_profile'],False)
            self.assertTrue(json.loads(out.getvalue())['ok'])
            result=subprocess.run([sys.executable,*args],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('inspection stopped',result.stderr)

    def test_cli_service_handoff_is_private_exclusive_and_does_not_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m,*_=self.fixture(root);manifest=info.registry.save(root,m)
            output=root/'services.json'
            base=[info.__file__,'--manifest',str(manifest),'--journal',str(root)]
            flags=['--service-config',str(output),'--issuer-name','identity.example.com',
                   '--known-hosts','/tmp/hosts','--tls-directory','/tmp/tls','--revision','a'*40,
                   '--source-repository','https://github.com/redhat-et/enmaas-mini.git','--external-database']
            config={'reviewed':'fixture'}
            with patch('sys.argv',base+flags),patch.object(info,'service_configuration',return_value=config) as prepare, \
                 patch.object(info.peers.vm,'Aws') as client,contextlib.redirect_stdout(io.StringIO()) as out:
                info.main()
            self.assertEqual(json.loads(output.read_text()),config)
            self.assertEqual(output.stat().st_mode&0o777,0o600)
            self.assertFalse(json.loads(out.getvalue())['services_installed'])
            self.assertIs(prepare.call_args.kwargs['external_database'],True)
            self.assertEqual(prepare.call_args.kwargs['population_count'],10000)
            client.assert_called_once_with(m['region'],m['aws_profile'],False)
            with patch('sys.argv',base+flags),patch.object(info,'service_configuration',return_value={'changed':True}), \
                 patch.object(info.peers.vm,'Aws'),self.assertRaises(FileExistsError):info.main()
            self.assertEqual(json.loads(output.read_text()),config)
            selected=list(flags);selected[selected.index(str(output))]=str(root/'selected.json')
            with patch('sys.argv',base+selected+['--population-count','20']),patch.object(info,'service_configuration',return_value=config) as prepare, \
                 patch.object(info.peers.vm,'Aws'),contextlib.redirect_stdout(io.StringIO()):info.main()
            self.assertEqual(prepare.call_args.kwargs['population_count'],20)
            for suffix in (['--issuer-name','identity.example.com'],['--external-database'],['--service-config',str(output)]):
                with patch('sys.argv',base+suffix),patch.object(info.peers.vm,'Aws') as client,self.assertRaises(ValueError):info.main()
                client.assert_not_called()


if __name__=='__main__':unittest.main()
