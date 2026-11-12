import importlib.util
from pathlib import Path
import unittest
import contextlib
import io
import tempfile
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('lab_dns',Path(__file__).with_name('dns.py'))
dns=importlib.util.module_from_spec(spec);spec.loader.exec_module(dns)

class Dns(unittest.TestCase):
    def setUp(self):
        self.manifest=dns.registry.new('123456789012','eu-central-1','/tmp/key','lab','Z123ABC','lab.example.com')
        self.addresses={'gateway':'1.1.1.1','keycloak':'8.8.8.8','observability':'9.9.9.9'}

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
                              ('plan', 'matching'), ('delete', 'matching')):
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

if __name__=='__main__':unittest.main()
