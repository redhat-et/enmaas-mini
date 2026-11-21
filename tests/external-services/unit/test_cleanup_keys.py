"""Key cleanup deletes only recorded IDs after verified VM/data removal."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('cleanup_keys',(Path(__file__).resolve().parents[1] / 'cleanup-keys.py'))
k=importlib.util.module_from_spec(spec);spec.loader.exec_module(k)

class Keys(unittest.TestCase):
    def fixture(self):
        manifest=k.t.inventory.registry.new('123456789012','eu-central-1','/tmp/key','operator',group='lab')
        journals={};rows=[]
        for i,(role,scenario) in enumerate(k.t.inventory.ROLES.items(),1):
            prefix=manifest['id']+'-'+role
            journals[role]={'AccountId':manifest['account'],'Region':manifest['region'],
                'Prefix':prefix,'Scenario':scenario,'InstanceId':'i-'+str(i)*8,
                'KeyPairName':prefix,'KeyPairId':'key-'+str(i)*17}
            rows.append({'KeyPairId':journals[role]['KeyPairId'],'KeyName':prefix,
                         'Tags':k.t.inventory.vm.tags(prefix,scenario)})
        return manifest,journals,rows

    def test_policy_has_only_exact_tagged_key_deletion_grants(self):
        m,j,rows=self.fixture()
        policy=k.policy(m,j)
        self.assertEqual(len(policy['Statement']),4)
        for grant,row in zip(policy['Statement'][1:],rows):
            self.assertEqual(grant['Action'],['ec2:DeleteKeyPair'])
            self.assertEqual(grant['Resource'],['arn:aws:ec2:'+m['region']+':'+m['account']+':key-pair/'+row['KeyName']])
            self.assertIn('aws:ResourceTag/enmaas:run',grant['Condition']['StringEquals'])

    def test_verify_observes_partial_journal_without_replaying_writes(self):
        m,j,rows=self.fixture();ids=sorted(row['KeyPairId'] for row in rows)
        events=[{'event':'plan','result':{'deployment':m['id'],'account':m['account'],
                  'region':m['region'],'key_pair_ids':ids}},
                {'event':'intent','action':'delete-key-pair','key_pair_id':ids[0]}]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700)
            for index,event in enumerate(events):
                path=root/(str(index).zfill(4)+'.json');path.write_text(json.dumps(event));path.chmod(0o600)
            for remaining in ([],rows[:1]):
                aws=Mock();aws.call.return_value={'KeyPairs':remaining}
                with patch.object(k.t.inventory.vm,'check_identity'):
                    result=k.verify(aws,m,j,root)
                self.assertEqual(result['deletion_verified'],not remaining)
                self.assertFalse(result['writes_performed']);aws.call.assert_called_once()
            for reply in ({'KeyPairs':[],'NextToken':'more'},{'KeyPairs':[rows[0],rows[0]]},
                          {'KeyPairs':[{'KeyPairId':'foreign'}]}):
                aws=Mock();aws.call.return_value=reply
                with patch.object(k.t.inventory.vm,'check_identity'),self.assertRaises(ValueError):
                    k.verify(aws,m,j,root)
            events[1]['key_pair_id']='foreign';(root/'0001.json').write_text(json.dumps(events[1]))
            with self.assertRaises(ValueError):k.verify(Mock(),m,j,root)
            (root/'0001.json').unlink()
            with self.assertRaises(ValueError):k.verify(Mock(),m,j,root)
            root.chmod(0o755)
            with self.assertRaises(ValueError):k.verify(Mock(),m,j,root)

    def test_completed_journal_and_binding_checks(self):
        m,j,rows=self.fixture();ids=sorted(row['KeyPairId'] for row in rows)
        with self.assertRaises(ValueError):k.recorded_keys(m,dict(j,unexpected=j['gateway']))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700)
            events=[{'event':'plan','result':{'deployment':m['id'],'account':m['account'],
                    'region':m['region'],'key_pair_ids':ids}}]
            for identity in ids:
                events.extend({'event':event,'action':'delete-key-pair','key_pair_id':identity}
                              for event in ('intent','request-recorded'))
            for index,event in enumerate(events):
                path=root/(str(index).zfill(4)+'.json');path.write_text(json.dumps(event));path.chmod(0o600)
            aws=Mock();aws.call.return_value={'KeyPairs':[]}
            with patch.object(k.t.inventory.vm,'check_identity'):
                self.assertTrue(k.verify(aws,m,j,root)['deletion_verified'])
            events[0]['result']['deployment']='other'
            (root/'0000.json').write_text(json.dumps(events[0]))
            with self.assertRaises(ValueError):k.verify(aws,m,j,root)

    def test_cli_policy_and_verification_are_read_only(self):
        import contextlib,io,sys
        m,j,_=self.fixture()
        base=['cleanup-keys','--manifest','manifest','--state-dir','states']
        for extra in (['--policy'],['--verify','--journal','keys'],['--verify'],[]):
            with patch.object(k.t.inventory.registry,'load',return_value=m), \
                 patch.object(k.t.inventory.registry,'read_private_json',side_effect=list(j.values())), \
                 patch.object(k,'Aws') as client,patch.object(k,'verify',return_value={'deletion_verified':True}), \
                 patch.object(sys,'argv',base+extra),contextlib.redirect_stdout(io.StringIO()) as output:
                if extra in (['--verify'],[]):
                    with self.assertRaises(ValueError):k.main()
                else:k.main()
                if extra==['--policy']:
                    client.assert_not_called();self.assertIn('Statement',json.loads(output.getvalue()))
                elif extra==['--verify','--journal','keys']:
                    client.assert_called_once_with(m)

    def test_client_never_allows_termination_or_unapproved_deletes(self):
        m,_,_=self.fixture()
        for apply,action in ((False,'delete-key-pair'),(True,'terminate-instances'),(True,'delete-security-group')):
            with self.assertRaises(ValueError):k.Aws(m,apply).call('ec2',action)
        with patch.object(k.t.subprocess,'run',return_value=Mock(returncode=0,stdout='{}')) as run:
            k.Aws(m,True).call('ec2','delete-key-pair',KeyPairId='key-11111111111111111')
        self.assertIn('delete-key-pair',run.call_args.args[0])

    def test_snapshot_rejects_missing_replaced_foreign_and_partial_keys(self):
        m,j,rows=self.fixture()
        for mode in ('ok','missing','duplicate','id','name','tags','page','live','owner','scope','instance-page'):
            import copy
            values=copy.deepcopy(rows);keys={'KeyPairs':values};instances={'Reservations':[]}
            if mode=='missing':values.pop()
            if mode=='duplicate':values.append(values[0])
            if mode=='id':values[0]['KeyPairId']='key-99999999999999999'
            if mode=='name':values[0]['KeyName']='foreign'
            if mode=='tags':values[0]['Tags']=[]
            if mode=='page':keys['NextToken']='remaining'
            if mode in ('live','owner','scope'):
                instances['Reservations']=[{'OwnerId':m['account'] if mode!='owner' else '999999999999',
                    'Instances':[{'InstanceId':'i-99999999','KeyName':rows[0]['KeyName'] if mode!='scope' else 'foreign',
                                  'State':{'Name':'running' if mode=='live' else 'terminated'}}]}]
            if mode=='instance-page':instances['NextToken']='remaining'
            aws=Mock();aws.call.side_effect=[keys,instances]
            with self.subTest(mode=mode):
                if mode=='ok':self.assertEqual(len(k.snapshot(aws,m,j)),3)
                else:
                    with self.assertRaises(ValueError):k.snapshot(aws,m,j)
        j['gateway'].pop('KeyPairId')
        with self.assertRaises(ValueError):k.snapshot(Mock(),m,j)

    def test_remaining_snapshot_accepts_only_terminated_key_users(self):
        m,j,rows=self.fixture();aws=Mock()
        aws.call.side_effect=[{'KeyPairs':rows[1:]},{'Reservations':[{'OwnerId':m['account'],
            'Instances':[{'KeyName':row['KeyName'],'State':{'Name':'terminated'}} for row in rows[1:]]}]}]
        self.assertEqual(k.snapshot(aws,m,j,deleted=[rows[0]['KeyPairId']]),
                         sorted(row['KeyPairId'] for row in rows[1:]))
        with self.assertRaises(ValueError):k.snapshot(Mock(),m,j,deleted=['foreign'])

    def test_cli_preview_apply_and_sanitized_entrypoint(self):
        import contextlib,io,sys,runpy
        m,j,_=self.fixture()
        base=['cleanup-keys','--manifest','manifest','--state-dir','states','--termination-journal','termination']
        for extra in ([],['--apply','--journal','keys','--confirm',k.required_confirmation(m)]):
            with patch.object(k.t.inventory.registry,'load',return_value=m), \
                 patch.object(k.t.inventory.registry,'read_private_json',side_effect=list(j.values())), \
                 patch.object(k,'Aws') as client,patch.object(k,'execute',return_value={'deletion_requested':bool(extra)}) as execute, \
                 patch.object(sys,'argv',base+extra),contextlib.redirect_stdout(io.StringIO()) as output:
                k.main()
            self.assertEqual(json.loads(output.getvalue()),{'deletion_requested':bool(extra)})
            self.assertEqual(execute.call_args.kwargs['apply'],bool(extra))
            client.assert_called_once_with(m,bool(extra))
        with patch.object(sys,'argv',base+['--apply']),self.assertRaisesRegex(SystemExit,'EC2 key cleanup stopped'):
            runpy.run_path(str((Path(__file__).resolve().parents[1] / 'cleanup-keys.py')),run_name='__main__')

    def test_preview_and_apply_journal_exact_ids_and_preserve_local_files(self):
        m,j,rows=self.fixture();expected=sorted(row['KeyPairId'] for row in rows)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700)
            aws=Mock();aws.call.side_effect=lambda service,action,**opts:{'Return':True,'KeyPairId':opts['KeyPairId']}
            with patch.object(k.t,'verify_termination',return_value={'termination_verified':True}), \
                 patch.object(k,'snapshot',side_effect=lambda *args,**kw:[v for v in expected if v not in kw.get('deleted',())]):
                result=k.execute(aws,m,j,root/'termination',root/'keys',apply=False,confirmation=None)
                self.assertFalse(result['deletion_requested']);aws.call.assert_not_called()
                self.assertFalse((root/'keys').exists())
                result=k.execute(aws,m,j,root/'termination',root/'keys',apply=True,confirmation=k.required_confirmation(m))
                self.assertTrue(result['deletion_requested'])
                self.assertEqual([call.kwargs for call in aws.call.call_args_list],[{'KeyPairId':v} for v in expected])
                self.assertEqual(len(list((root/'keys').glob('*.json'))),7)
                before=aws.call.call_count
                with self.assertRaises(FileExistsError):
                    k.execute(aws,m,j,root/'termination',root/'keys',apply=True,confirmation=k.required_confirmation(m))
                self.assertEqual(aws.call.call_count,before)

    def test_uncertain_outcome_stops_without_retry_and_keeps_intent(self):
        m,j,rows=self.fixture();expected=sorted(row['KeyPairId'] for row in rows)
        for response in ({}, {'Return':True,'KeyPairId':'foreign'},ValueError('SECRET')):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);root.chmod(0o700);aws=Mock()
                aws.call.side_effect=response if isinstance(response,Exception) else [response]
                with patch.object(k.t,'verify_termination',return_value={'termination_verified':True}), \
                     patch.object(k,'snapshot',side_effect=lambda *args,**kw:[v for v in expected if v not in kw.get('deleted',())]),self.assertRaisesRegex(ValueError,'uncertain') as caught:
                    k.execute(aws,m,j,root/'termination',root/'keys',apply=True,confirmation=k.required_confirmation(m))
                self.assertNotIn('SECRET',str(caught.exception));self.assertEqual(aws.call.call_count,1)
                self.assertEqual(json.loads((root/'keys/0001.json').read_text())['event'],'intent')

    def test_unverified_termination_wrong_confirmation_and_drift_block_deletion(self):
        m,j,rows=self.fixture();expected=sorted(row['KeyPairId'] for row in rows)
        for mode in ('confirmation','termination','drift'):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);root.chmod(0o700);aws=Mock()
                with patch.object(k.t,'verify_termination',return_value={'termination_verified':mode!='termination'}), \
                     patch.object(k,'snapshot',side_effect=[expected,[]]),self.assertRaises(ValueError):
                    k.execute(aws,m,j,root/'termination',root/'keys',apply=True,
                              confirmation='wrong' if mode=='confirmation' else k.required_confirmation(m))
                aws.call.assert_not_called()

if __name__=='__main__':unittest.main()
