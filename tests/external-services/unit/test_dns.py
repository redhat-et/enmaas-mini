import importlib.util
from pathlib import Path
import unittest
import contextlib
import io
import tempfile
import json
import subprocess
import sys
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('lab_dns',(Path(__file__).resolve().parents[1] / 'dns.py'))
dns=importlib.util.module_from_spec(spec);spec.loader.exec_module(dns)

class Dns(unittest.TestCase):
    def setUp(self):
        self.manifest=dns.registry.new('123456789012','eu-central-1','/tmp/key','lab','Z123ABC','lab.example.com')
        self.addresses={'gateway':'1.1.1.1','keycloak':'8.8.8.8','observability':'9.9.9.9'}

    def test_programmatic_validation_uses_value_error(self):
        with self.assertRaises(ValueError):dns.invalid_operation('invalid request')

    def test_records_have_exact_scope_and_owner_nonce(self):
        records=dns.records(self.manifest,self.addresses,'a'*32)
        self.assertEqual(len(records),4)
        self.assertTrue(all(self.manifest['id']+'.lab.example.com.' in r['Name'] for r in records))
        self.assertEqual([r['Type'] for r in records],['A','A','A','TXT'])
        self.assertIn('a'*32,records[-1]['ResourceRecords'][0]['Value'])

    def test_rejects_partial_or_private_addresses(self):
        for addresses in ({'gateway':'1.1.1.1'},{**self.addresses,'gateway':'127.0.0.1'},
                          {**self.addresses,'gateway':'10.0.0.1'}):
            with self.assertRaises(ValueError):dns.records(self.manifest,addresses,'a'*32)

    def test_delete_requires_exact_live_record_values_and_owner(self):
        records=dns.records(self.manifest,self.addresses,'a'*32)
        self.assertEqual(len(dns.delete_batch(records,records)['Changes']),4)
        for index in range(4):
            import copy
            changed=copy.deepcopy(records);changed[index]['TTL']=61
            with self.assertRaises(ValueError):dns.delete_batch(records,changed)
        with self.assertRaises(ValueError):dns.delete_batch(records,records[:-1])

    def test_zone_must_be_public_and_parent_of_requested_domain(self):
        zone={'Id':'/hostedzone/Z123ABC','Name':'example.com.','Config':{'PrivateZone':False}}
        dns.validate_zone(self.manifest,zone)
        for changed in ({**zone,'Name':'ample.com.'},{**zone,'Config':{'PrivateZone':True}},
                        {**zone,'Id':'/hostedzone/ZOTHER'}):
            with self.assertRaises(ValueError):dns.validate_zone(self.manifest,changed)

    def test_create_journals_before_write_and_refuses_wrong_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);manifest=dns.registry.save(root/'registry',self.manifest)
            journal=root/'dns.json'
            args=['dns.py','create','--manifest',str(manifest),'--journal',str(journal),
                  '--confirm','create '+self.manifest['id']+' '+self.manifest['account']]
            for role,address in self.addresses.items():args+=['--'+role+'-ip',address]
            calls=[]
            def call(service,action,**options):
                calls.append(action)
                if action=='get-caller-identity':return {'Account':self.manifest['account'],'Arn':'arn:aws:iam::123456789012:user/operator'}
                if action=='get-hosted-zone':return {'HostedZone':{'Id':'Z123ABC','Name':'example.com.','Config':{'PrivateZone':False}}}
                if action=='list-resource-record-sets':return {'ResourceRecordSets':[]}
                self.assertEqual(action,'change-resource-record-sets')
                saved=dns.load_journal(journal,self.manifest)
                self.assertEqual([c['ResourceRecordSet'] for c in options['ChangeBatch']['Changes']],saved['records'])
                self.assertTrue(all(c['Action']=='CREATE' for c in options['ChangeBatch']['Changes']))
                raise ValueError('simulated uncertain API result')
            aws=Mock();aws.call.side_effect=call
            with patch.object(dns,'Aws',return_value=aws),patch('sys.argv',args):
                with self.assertRaises(ValueError):dns.main()
            self.assertTrue(journal.exists())
            self.assertEqual(calls.count('change-resource-record-sets'),1)
            aws.call.reset_mock();aws.call.side_effect=lambda *a,**k:{'Account':'999999999999','Arn':'other'}
            with patch.object(dns,'Aws',return_value=aws),patch('sys.argv',args):
                with self.assertRaises(ValueError):dns.main()
            self.assertEqual(aws.call.call_count,1)

    def test_command_refuses_drift_and_keeps_read_only_actions_read_only(self):
        import copy
        for action, state in (('delete', 'drift'), ('delete', 'absent'),
                              ('create', 'drift'), ('status', 'matching'),
                              ('plan', 'matching'), ('delete', 'matching'), ('create', 'matching')):
            with self.subTest(action=action, state=state), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                manifest=dns.registry.save(root/'registry',self.manifest)
                journal=root/'dns.json'
                saved={'version':1,'manifest':self.manifest,'addresses':self.addresses,'nonce':'a'*32}
                saved['records']=dns.records(self.manifest,self.addresses,saved['nonce'])
                dns.write_journal(journal,saved)
                live=copy.deepcopy(saved['records'])
                if state=='drift':
                    live[-1]['ResourceRecords']=[{'Value':'"different-owner"'}]
                elif state=='absent':live=[]
                writes=[]
                def call(service,operation,**options):
                    if operation=='get-caller-identity':
                        return {'Account':self.manifest['account'],'Arn':'arn:aws:iam::123456789012:user/operator'}
                    if operation=='get-hosted-zone':
                        return {'HostedZone':{'Id':'Z123ABC','Name':'example.com.','Config':{'PrivateZone':False}}}
                    if operation=='list-resource-record-sets':
                        return {'ResourceRecordSets':[r for r in live if r['Name']==options['StartRecordName']]}
                    self.assertEqual(operation,'change-resource-record-sets')
                    writes.append(options['ChangeBatch'])
                    return {'ChangeInfo':{'Id':'test-change','Status':'PENDING'}}
                aws=Mock();aws.call.side_effect=call
                args=['dns.py',action,'--manifest',str(manifest),'--journal',str(journal),
                      '--confirm',action+' '+self.manifest['id']+' '+self.manifest['account']]
                with patch.object(dns,'Aws',return_value=aws),patch('sys.argv',args),contextlib.redirect_stdout(io.StringIO()):
                    if state=='drift':
                        with self.assertRaises(ValueError):dns.main()
                    else:dns.main()
                if action=='delete' and state=='matching':
                    self.assertEqual(writes,[dns.delete_batch(saved['records'],live)])
                else:self.assertEqual(writes,[])
                self.assertEqual(dns.load_journal(journal,self.manifest),saved)

    def test_journal_cannot_change_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'dns.json'
            journal={'version':1,'manifest':self.manifest,'addresses':self.addresses,'nonce':'a'*32}
            journal['records']=dns.records(self.manifest,self.addresses,journal['nonce'])
            journal['records'][0]['Name']='unrelated.example.com.'
            dns.write_journal(path,journal)
            with self.assertRaises(ValueError):dns.load_journal(path,self.manifest)

    def test_aws_wrapper_scopes_commands_and_redacts_failure(self):
        aws=dns.Aws(self.manifest)
        with patch.object(dns.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'not allowed'):
                aws.call('route53','delete-hosted-zone')
            run.assert_not_called()
            run.return_value=subprocess.CompletedProcess([],0,'{"ok":true}','')
            self.assertEqual(aws.call('route53','get-hosted-zone',Id='Z123ABC'),{'ok':True})
            command=run.call_args.args[0]
            self.assertEqual(command[command.index('--profile')+1],self.manifest['aws_profile'])
            self.assertEqual(command[command.index('--region')+1],self.manifest['region'])
            self.assertEqual(json.loads(command[-1]),{'Id':'Z123ABC'})
            self.assertEqual(run.call_args.kwargs['timeout'],60)
            self.assertEqual(run.call_args.kwargs['env']['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
            self.assertNotIn('AWS_ACCESS_KEY_ID',run.call_args.kwargs['env'])
            run.return_value=subprocess.CompletedProcess([],1,'','synthetic-sensitive-aws-error')
            with self.assertRaises(ValueError) as caught:
                aws.call('route53','get-hosted-zone',Id='Z123ABC')
            self.assertNotIn('synthetic-sensitive',str(caught.exception))
            run.return_value=subprocess.CompletedProcess([],0,'not-json','')
            with self.assertRaises(ValueError):
                aws.call('sts','get-caller-identity')

    def test_public_parent_and_existing_journal_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp)/'public'
            parent.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError,'private user-owned'):
                dns.write_journal(parent/'dns.json',{})
            parent.chmod(0o700)
            path=parent/'dns.json'
            dns.write_journal(path,{'original':True})
            with self.assertRaises(FileExistsError):
                dns.write_journal(path,{'replacement':True})
            self.assertEqual(json.loads(path.read_text()),{'original':True})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaisesRegex(ValueError,'different deployment'):
                dns.load_journal(path,self.manifest)

    def test_main_requires_zone_journal_matching_addresses_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            manifest=dns.registry.save(root/'registry',self.manifest)
            journal=root/'dns.json'
            base=['dns.py','status','--manifest',str(manifest),'--journal',str(journal)]
            with patch.object(dns,'Aws') as aws,patch('sys.argv',base),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):dns.main()
                aws.assert_not_called()
            nozone=dict(self.manifest,zone_id=None,domain=None)
            with patch.object(dns.registry,'load',return_value=nozone),patch.object(dns,'Aws') as aws,patch('sys.argv',base),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):dns.main()
                aws.assert_not_called()
            saved={'version':1,'manifest':self.manifest,'addresses':self.addresses,'nonce':'a'*32}
            saved['records']=dns.records(self.manifest,self.addresses,saved['nonce'])
            dns.write_journal(journal,saved)
            with patch.object(dns,'Aws') as aws,patch('sys.argv',base+['--gateway-ip','8.8.4.4']),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):dns.main()
                aws.assert_not_called()
            aws=Mock()
            aws.call.side_effect=[{'Account':self.manifest['account'],'Arn':'arn:aws:iam::123456789012:user/operator'},
                {'HostedZone':{'Id':'Z123ABC','Name':'example.com.','Config':{'PrivateZone':False}}}]
            base[1]='delete'
            with patch.object(dns,'Aws',return_value=aws),patch.object(dns,'live_records',return_value=saved['records']),patch('sys.argv',base),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):dns.main()
            self.assertEqual(aws.call.call_count,2)
            aws.call.side_effect=None
            aws.call.return_value={'Account':self.manifest['account'],'Arn':'arn:aws:iam::123456789012:root'}
            with patch.object(dns,'Aws',return_value=aws),patch('sys.argv',base):
                with self.assertRaisesRegex(ValueError,'root credentials'):dns.main()

    def test_cli_invalid_manifest_reports_failure_without_aws(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,str((Path(__file__).resolve().parents[1] / 'dns.py')),'status',
                '--manifest',str(Path(tmp)/'absent'),'--journal',str(Path(tmp)/'dns.json')],
                capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('DNS operation stopped:',result.stderr)

if __name__=='__main__':unittest.main()
