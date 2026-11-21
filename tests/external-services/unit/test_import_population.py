"""Partner mapping preserves issuer UUIDs without credential or role writes."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('import_population',(Path(__file__).resolve().parents[1] / 'import-population.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)


class Import(unittest.TestCase):
    def document(self):
        prefix='perf-'+'a'*16
        return {'version':1,'population':prefix,'profiles':[{'user_id':str(uuid.uuid4()),
            'tags':{'email':prefix+'-'+str(i).zfill(5)+'@example.test','first_name':'Synthetic','last_name':'User'}} for i in (1,2)]}

    def test_api_receipts_preserve_uuid_and_resume_without_replaying_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document();api=Mock(side_effect=lambda profile:profile)
            result=p.apply(doc,root,api)
            self.assertEqual(result,{'recorded_imports':2,'roles_configured':False,'budgets_configured':False})
            p.apply(doc,root,api);self.assertEqual(api.call_count,2)
            self.assertEqual(api.call_args_list[0].args[0],doc['profiles'][0])
            with self.assertRaises(ValueError):p.apply(self.document(),root,api)

    def test_retirement_requires_confirmed_imports_and_cannot_reactivate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document();retire=Mock(side_effect=lambda profile:{'user_id':profile['user_id'],'active':False,'keys_revoked':2})
            with self.assertRaises(ValueError):p.apply(doc,root,retire,retire=True)
            retire.assert_not_called();self.assertFalse(root.exists())
            p.apply(doc,root,lambda profile:profile)
            self.assertEqual(p.apply(doc,root,retire,retire=True),{'recorded_retirements':2,'issuer_accounts_removed':False})
            p.apply(doc,root,retire,retire=True);self.assertEqual(retire.call_count,2)
            with self.assertRaises(ValueError):p.apply(doc,root,Mock())

    def test_uncertain_or_incomplete_retirement_stops_without_retry(self):
        for result in ({'active':True,'keys_revoked':0},{'active':False,'keys_revoked':True},
                       {'active':False,'keys_revoked':-1}):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'receipts';doc=self.document();p.apply(doc,root,lambda profile:profile)
                retire=Mock(side_effect=lambda profile:dict(result,user_id=profile['user_id']))
                with self.assertRaises(ValueError):p.apply(doc,root,retire,retire=True)
                with self.assertRaises(ValueError):p.apply(doc,root,retire,retire=True)
                self.assertEqual(retire.call_count,1)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document();p.apply(doc,root,lambda profile:profile)
            (root/(doc['profiles'][0]['user_id']+'.json')).unlink()
            with self.assertRaises(ValueError):p.apply(doc,root,Mock(),retire=True)

    def test_all_key_cleanup_checks_identity_and_suspension_before_revoking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document()
            p.apply(doc,root,lambda profile:profile)
            with self.assertRaises(ValueError):p.clear_keys(doc,root,Mock(),execute=True)
            p.apply(doc,root,lambda profile:{'user_id':profile['user_id'],'active':False,'keys_revoked':0},retire=True)
            users=[{'user_id':profile['user_id'],'active':False,'logins':[profile['tags']['email']]} for profile in doc['profiles']]
            api=Mock(side_effect=[users,{'count':2,'dry_run':True},{'count':0,'dry_run':True}])
            self.assertEqual(p.clear_keys(doc,root,api),{'users_checked':2,'active_keys':2,'changed':False})
            self.assertTrue(all(call.args[2]['dry_run'] for call in api.call_args_list[1:]))
            api=Mock(side_effect=[users,{'count':2,'dry_run':False},{'count':0,'dry_run':True},
                                  {'count':0,'dry_run':False},{'count':0,'dry_run':True}])
            self.assertEqual(p.clear_keys(doc,root,api,execute=True),{'users_checked':2,'active_keys':0,'changed':True})
            api=Mock(side_effect=[users,{'count':0,'dry_run':True},{'count':0,'dry_run':True}])
            p.clear_keys(doc,root,api,execute=True)
            self.assertTrue(all(call.args[2]['dry_run'] for call in api.call_args_list[1:]))
            for bad in ([],[dict(users[0],active=True),users[1]],[dict(users[0],logins=['other']),users[1]],
                        [users[0],users[0],users[1]]):
                api=Mock(return_value=bad)
                with self.assertRaises(ValueError):p.clear_keys(doc,root,api,execute=True)
                self.assertEqual(api.call_count,1)

    def test_key_cleanup_failures_are_bounded_and_never_replayed(self):
        for bad in (None,{'count':True,'dry_run':False},{'count':-1,'dry_run':False},
                    {'count':0,'dry_run':True},RuntimeError('credential-sentinel')):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'receipts';doc=self.document()
                p.apply(doc,root,lambda profile:profile)
                p.apply(doc,root,lambda profile:dict(user_id=profile['user_id'],active=False,keys_revoked=0),retire=True)
                users=[dict(user_id=x['user_id'],active=False,logins=[x['tags']['email']]) for x in doc['profiles']]
                api=Mock(side_effect=[users,bad])
                with self.assertRaises(ValueError) as caught:p.clear_keys(doc,root,api,execute=True)
                self.assertNotIn('credential-sentinel',str(caught.exception))
                retry=Mock(return_value=users)
                with self.assertRaises(ValueError):p.clear_keys(doc,root,retry,execute=True)
                self.assertEqual(retry.call_count,1)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document()
            p.apply(doc,root,lambda profile:profile)
            p.apply(doc,root,lambda profile:dict(user_id=profile['user_id'],active=False,keys_revoked=0),retire=True)
            with self.assertRaises(ValueError):p.clear_keys(self.document(),root,Mock())
            with self.assertRaises(ValueError):p.clear_keys(doc,root,Mock(return_value={}))
            users=[dict(user_id=x['user_id'],active=False,logins=[x['tags']['email']]) for x in doc['profiles']]
            api=Mock(side_effect=[users,{'count':0,'dry_run':False},{'count':1,'dry_run':True}])
            with self.assertRaises(ValueError):p.clear_keys(doc,root,api,execute=True)

    def test_population_access_uses_existing_user_identity_and_preserves_finite_keys(self):
        from datetime import datetime,timedelta,timezone
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document();p.apply(doc,root,lambda profile:profile)
            users=[dict(user_id=x['user_id'],active=True,role='user',logins=[x['tags']['email']]) for x in doc['profiles']]
            expires=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
            key=lambda n:dict(id='key-'+str(n),key='synthetic-private-key-'+str(n),expirationDate=expires)
            api=Mock(side_effect=[users,[],{'updated':True},key(1),{'updated':True},key(2)])
            result=p.configure_access(doc,root,api)
            self.assertEqual(result,{'configured_users':2,'monthly_usd':5,'key_lifetime':'24h'})
            self.assertNotIn('synthetic-private',str(result))
            first=doc['profiles'][0]['user_id']
            self.assertEqual((root/(first+'.key')).read_text(),'synthetic-private-key-1\n')
            self.assertEqual((root/(first+'.key')).stat().st_mode&0o777,0o600)
            self.assertNotIn('synthetic-private',(root/(first+'.access.json')).read_text())
            self.assertTrue(all(call.args[1] in ('users','budgets','keys') for call in api.call_args_list))
            budgets=[dict(scope='user',principal=x['tags']['email'],monthly_usd=5) for x in doc['profiles']]
            repeated=Mock(side_effect=[users,budgets])
            self.assertEqual(p.configure_access(doc,root,repeated),result)
            self.assertEqual(repeated.call_count,2)
            with self.assertRaises(ValueError):p.configure_access(doc,root,Mock(side_effect=[users,budgets]),monthly_usd=6)
            badusers=[dict(users[0],role='super_admin'),users[1]]
            api=Mock(return_value=badusers)
            with self.assertRaises(ValueError):p.configure_access(doc,root,api)
            self.assertEqual(api.call_count,1)
            p.apply(doc,root,lambda profile:dict(user_id=profile['user_id'],active=False,keys_revoked=1),retire=True)
            api=Mock()
            with self.assertRaises(ValueError):p.configure_access(doc,root,api)
            api.assert_not_called()

    def test_access_rejects_invalid_inputs_before_writes(self):
        for value in (True,float('nan'),.001,10001):
            with self.assertRaises(ValueError):p.configure_access(self.document(),Path('/missing'),Mock(),monthly_usd=value)
        with self.assertRaises(ValueError):p.configure_access(self.document(),Path('/missing'),Mock(),key_lifetime='forever')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document();p.apply(doc,root,lambda profile:profile)
            with self.assertRaises(ValueError):p.configure_access(self.document(),root,Mock())
            users=[dict(user_id=x['user_id'],active=True,role='user',logins=[x['tags']['email']]) for x in doc['profiles']]
            for bad in (None,[None],[dict(users[0],logins=None)],[],users+[users[0]],
                        [dict(users[0],active=False),users[1]],[dict(users[0],logins=['other']),users[1]]):
                api=Mock(return_value=bad)
                with self.assertRaises(ValueError):p.configure_access(doc,root,api)
                self.assertEqual(api.call_count,1)
            for bad in (None,[None],[dict(scope='user',principal='x',monthly_usd=True)]):
                api=Mock(side_effect=[users,bad])
                with self.assertRaises(ValueError):p.configure_access(doc,root,api)
                self.assertEqual(api.call_count,2)
            api=Mock(side_effect=[users,[dict(scope='user',principal=doc['profiles'][0]['tags']['email'],monthly_usd=5)]])
            with self.assertRaises(ValueError):p.configure_access(doc,root,api)
            self.assertEqual(api.call_count,2)

    def test_uncertain_access_never_reissues_and_keeps_errors_private(self):
        from datetime import datetime,timedelta,timezone
        expires=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        key={'id':'key-1','key':'private-sentinel','expirationDate':expires}
        cases=[({'updated':1},key),({'updated':False},key),(RuntimeError('private-sentinel'),key)]
        cases += [({'updated':True},response) for response in (None,{},dict(key,key='has space'),
                  dict(key,key='é'),dict(key,expirationDate=None),dict(key,expirationDate='invalid'),
                  dict(key,expirationDate='2020-01-01T00:00:00'),dict(key,expirationDate='2020-01-01T00:00:00Z'),
                  dict(key,expirationDate=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()))]
        for budget,key_result in cases:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'receipts';doc=self.document();doc['profiles']=doc['profiles'][:1]
                p.apply(doc,root,lambda profile:profile)
                profile=doc['profiles'][0];users=[dict(user_id=profile['user_id'],active=True,role='user',logins=[profile['tags']['email']])]
                api=Mock(side_effect=[users,[],budget,key_result])
                with self.assertRaises(ValueError) as caught:p.configure_access(doc,root,api,key_lifetime='1h',list_keys=Mock(return_value=[]))
                self.assertNotIn('private-sentinel',str(caught.exception))
                retry=Mock(side_effect=[users,[]])
                with self.assertRaises(ValueError):p.configure_access(doc,root,retry,key_lifetime='1h')
                self.assertEqual(retry.call_count,2)

    def test_missing_create_expiry_requires_identity_scoped_active_key_readback(self):
        from datetime import datetime,timedelta,timezone
        for rows,success in (([],False),([{'id':'key-1','status':'revoked'}],False),([{'id':'key-1','status':'active'}],False),
                             ([{'id':'key-1','status':'active','expirationDate':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}],True)):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'receipts';doc=self.document();doc['profiles']=doc['profiles'][:1]
                p.apply(doc,root,lambda profile:profile);profile=doc['profiles'][0]
                users=[dict(user_id=profile['user_id'],active=True,role='user',logins=[profile['tags']['email']])]
                api=Mock(side_effect=[users,[],{'updated':True},{'id':'key-1','key':'private-sentinel'}])
                with patch.object(p.oidc,'list_keys',return_value=rows) as readback:
                    if success:p.configure_access(doc,root,api)
                    else:
                        with self.assertRaises(ValueError):p.configure_access(doc,root,api)
                readback.assert_called_once_with(profile)

    def test_saved_key_drift_stops_without_replacement(self):
        from datetime import datetime,timedelta,timezone
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'receipts';doc=self.document();doc['profiles']=doc['profiles'][:1]
            p.apply(doc,root,lambda profile:profile)
            profile=doc['profiles'][0];identity=profile['user_id'];login=profile['tags']['email']
            users=[dict(user_id=identity,active=True,role='user',logins=[login])]
            expires=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
            api=Mock(side_effect=[users,[{'scope':'group'}],{'updated':True},{'id':'key-1','key':'private-sentinel','expirationDate':expires}])
            p.configure_access(doc,root,api)
            path=root/(identity+'.key');path.write_text('different-key\n')
            api=Mock(side_effect=[users,[dict(scope='user',principal=login,monthly_usd=5)]])
            with self.assertRaises(ValueError):p.configure_access(doc,root,api)
            self.assertEqual(api.call_count,2)

    def test_uncertain_api_stops_and_does_not_retry_or_continue(self):
        for response in (ValueError('credential-sentinel'),{'user_id':str(uuid.uuid4())},None):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'receipts';doc=self.document()
                api=Mock(side_effect=response) if isinstance(response,Exception) else Mock(return_value=response)
                with self.assertRaises(ValueError) as caught:p.apply(doc,root,api)
                self.assertNotIn('credential-sentinel',str(caught.exception))
                self.assertEqual(api.call_count,1)
                with self.assertRaisesRegex(ValueError,'Uncertain'):p.apply(doc,root,api)
                self.assertEqual(api.call_count,1)
                self.assertFalse((root/(doc['profiles'][1]['user_id']+'.json')).exists())

    def test_invalid_or_secret_bearing_exports_rejected_before_state_allocation(self):
        doc=self.document()
        for bad in ([],{},dict(doc,profiles=[]),dict(doc,password='secret'),dict(doc,population='foreign')):
            with self.assertRaises(ValueError):p.validate(bad)
        for change in ('duplicate','email','password'):
            bad=self.document()
            if change=='duplicate':bad['profiles'][1]['user_id']=bad['profiles'][0]['user_id']
            elif change=='email':bad['profiles'][0]['tags']['email']='someone@example.test'
            else:bad['profiles'][0]['password']='secret'
            with self.assertRaises(ValueError):p.validate(bad)

    def test_cli_root_confirmation_and_existing_provisioner(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=[str(Path(p.__file__)),'--profiles',str(Path(tmp)/'profiles.json'),'--state',str(Path(tmp)/'receipts')]
            with patch('sys.argv',args),self.assertRaises(ValueError):p.main()
            with patch('sys.argv',args+['--yes']),patch.object(p.os,'geteuid',return_value=0), \
                 patch.object(p.private,'read',return_value=self.document()),patch.object(p,'apply',return_value={'recorded_imports':2}) as apply,contextlib.redirect_stdout(io.StringIO()) as out:
                p.main()
            self.assertEqual(apply.call_args.args[2],p.oidc.provision)
            self.assertIn('recorded_imports',out.getvalue())
            with patch('sys.argv',args+['--yes','--retire']),patch.object(p.os,'geteuid',return_value=0), \
                 patch.object(p.private,'read',return_value=self.document()),patch.object(p,'apply',return_value={'recorded_retirements':2}) as apply,contextlib.redirect_stdout(io.StringIO()):
                p.main()
            self.assertEqual(apply.call_args.args[2],p.oidc.deactivate)
            self.assertIs(apply.call_args.kwargs['retire'],True)
            with patch('sys.argv',args+['--revoke-all-keys']),patch.object(p.os,'geteuid',return_value=0), \
                 patch.object(p.private,'read',return_value=self.document()),patch.object(p,'clear_keys',return_value={'active_keys':0}) as cleanup,contextlib.redirect_stdout(io.StringIO()):
                p.main()
            self.assertIs(cleanup.call_args.kwargs['execute'],False)
            with patch('sys.argv',args+['--configure-access','--yes','--monthly-usd','7','--key-lifetime','1h']),patch.object(p.os,'geteuid',return_value=0), \
                 patch.object(p.private,'read',return_value=self.document()),patch.object(p,'configure_access',return_value={'configured_users':2}) as access,contextlib.redirect_stdout(io.StringIO()):
                p.main()
            self.assertEqual(access.call_args.kwargs,{'monthly_usd':7,'key_lifetime':'1h'})
            result=subprocess.run([sys.executable,*args],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,'')


if __name__=='__main__':unittest.main()
