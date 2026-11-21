"""Persona bootstrap preserves issuer UUIDs and refuses unsafe privilege replay."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid
import contextlib
import io
import json
import runpy
import subprocess
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('personas',(Path(__file__).resolve().parents[1] / 'personas.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class Personas(unittest.TestCase):
    def setUp(self):
        self.identities={name:str(uuid.uuid4()) for name in p.NAMES}
        self.users=[];self.budgets=[];self.writes=[]
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'state'

    def partner(self,profile):
        self.writes.append(('import',profile['user_id']))
        row={'user_id':profile['user_id'],'logins':[profile['tags']['email']],
             'active':True,'role':'user'}
        self.users.append(row);return copy.deepcopy(row)

    def management(self,method,resource,body=None):
        if method=='GET':return copy.deepcopy(self.users if resource=='users' else self.budgets)
        self.writes.append(('budget',body['subject']))
        self.budgets.append({'scope':'user','principal':body['subject'],'monthly_usd':body['monthly_usd']})
        return {'updated':True}

    def public(self,method,path,body):
        self.assertEqual(method,'PATCH');self.assertTrue(path.startswith('/api/v1/admin/partner-users/'))
        identity=path.rsplit('/',1)[1];row=next(r for r in self.users if r['user_id']==identity)
        self.writes.append(('access',identity));row.update(body);return copy.deepcopy(row)

    def run_setup(self):return p.apply(self.identities,self.root,self.partner,self.management,self.public)

    def test_full_setup_and_no_write_resume(self):
        result=self.run_setup()
        self.assertTrue(result['dashboard_personas_configured']);self.assertFalse(result['inference_keys_created'])
        self.assertEqual(len(self.writes),8)
        bob=next(row for row in self.users if row['user_id']==self.identities['bob'])
        self.assertEqual(bob['manager_user_id'],self.identities['alice'])
        self.assertEqual(self.run_setup(),result);self.assertEqual(len(self.writes),8)

    def test_invalid_or_duplicate_uuid_refused_without_state(self):
        for values in ({},dict(self.identities,bob=self.identities['alice']),dict(self.identities,bob='invalid'),dict(self.identities,bob=42)):
            with self.assertRaises(ValueError):p.apply(values,self.root,self.partner,self.management,self.public)
        self.assertFalse(self.root.exists());self.assertFalse(self.writes)

    def test_existing_login_cannot_be_adopted_or_promoted(self):
        self.users=[{'user_id':str(uuid.uuid4()),'logins':['admin@example.test'],'active':True}]
        with self.assertRaises(ValueError):self.run_setup()
        self.assertFalse(self.writes)

    def test_existing_matching_identity_requires_reconciliation(self):
        self.partner(p.profiles(self.identities)['admin']);self.writes.clear()
        with self.assertRaises(ValueError):self.run_setup()
        self.assertFalse(self.writes)

    def test_existing_budget_is_not_overwritten(self):
        self.budgets=[{'scope':'user','principal':'alice@example.test','monthly_usd':100}]
        with self.assertRaises(ValueError):self.run_setup()
        self.assertFalse(self.writes)

    def test_uncertain_privilege_write_not_replayed(self):
        def fail(*args):self.public(*args);raise TimeoutError('private response')
        with self.assertRaisesRegex(ValueError,'uncertain: access-admin'):
            p.apply(self.identities,self.root,self.partner,self.management,fail)
        count=len(self.writes)
        with self.assertRaisesRegex(ValueError,'Uncertain'):self.run_setup()
        self.assertEqual(len(self.writes),count)

    def test_role_drift_is_not_silently_repaired(self):
        self.run_setup();self.users[1]['role']='super_admin'
        with self.assertRaisesRegex(ValueError,'access changed'):self.run_setup()
        self.assertEqual(len(self.writes),8)

    def test_issuer_binding_cannot_change(self):
        self.run_setup();self.identities['bob']=str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError,'another issuer'):self.run_setup()
        self.assertEqual(len(self.writes),8)

    def test_missing_identity_and_budget_drift_refused(self):
        self.run_setup();saved=self.users.pop()
        with self.assertRaisesRegex(ValueError,'import is missing'):self.run_setup()
        self.users.append(saved);self.budgets[0]['monthly_usd']=100
        with self.assertRaisesRegex(ValueError,'budget differs'):self.run_setup()
        self.assertEqual(len(self.writes),8)

    def test_invalid_user_readbacks(self):
        good={'user_id':self.identities['admin'],'logins':['admin@example.test'],'active':True}
        for rows in (None,[None],[{}],[good,good],[dict(good,active=False)],[dict(good,logins=['other'])]):
            with self.assertRaises(ValueError):p.verify_users(rows,self.identities)
        with self.assertRaisesRegex(ValueError,'missing'):p.verify_users([],self.identities,complete=True)

    def test_unrecorded_identity_after_partial_setup(self):
        self.root.mkdir(mode=0o700);p.state.save(self.root,'identities.json',self.identities)
        p.state.save(self.root,'import-admin.json',{'state':'done','step':'import-admin'})
        for name in ('admin','alice'):self.partner(p.profiles(self.identities)[name])
        self.writes.clear()
        with self.assertRaisesRegex(ValueError,'Unrecorded'):self.run_setup()
        self.assertFalse(self.writes)

    def test_invalid_budget_readback_stops_before_writes(self):
        for budgets in (None,[None]):
            self.budgets=budgets
            with self.assertRaisesRegex(ValueError,'Invalid budget'):self.run_setup()
        self.assertFalse(self.writes)

    def test_disappearing_import_prevents_role_assignment(self):
        def management(method,resource,body=None):
            if method=='GET' and resource=='users' and len(self.users)==3:return self.users[:2]
            return self.management(method,resource,body)
        with self.assertRaisesRegex(ValueError,'All issuer identities'):
            p.apply(self.identities,self.root,self.partner,management,self.public)
        self.assertTrue(all(kind=='import' for kind,_ in self.writes))

    def test_mismatched_write_responses_leave_uncertain_receipt(self):
        for stage in ('import','access','budget'):
            with self.subTest(stage=stage):
                root=Path(self.tmp.name)/stage;self.users=[];self.budgets=[]
                partner=(lambda profile: {}) if stage=='import' else self.partner
                public=(lambda *args: {}) if stage=='access' else self.public
                def management(method,resource,body=None):
                    if stage=='budget' and method=='PUT':return {'updated':False}
                    return self.management(method,resource,body)
                with self.assertRaisesRegex(ValueError,'uncertain'):
                    p.apply(self.identities,root,partner,management,public)
                receipt=p.state.load(root,stage+'-'+('alice' if stage=='budget' else 'admin')+'.json')
                self.assertEqual(receipt['state'],'started')

    def test_public_client_uses_private_files_verified_loopback_and_no_output(self):
        call=p.public_client('https://192.0.2.1:8443')
        def run(command,**kwargs):
            self.assertEqual(command[command.index('--url')+1],'https://127.0.0.1:8443')
            self.assertEqual(kwargs['stdout'],subprocess.DEVNULL);self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
            self.assertNotIn('subject-sentinel',repr(command))
            output=Path(command[command.index('--output')+1])
            p.state.save(output.parent,output.name,{'updated':True},exclusive=True)
            return Mock(returncode=0)
        with patch.object(p.subprocess,'run',side_effect=run):
            self.assertEqual(call('PATCH','/api/v1/admin/partner-users/id',{'subject':'subject-sentinel'}),{'updated':True})
        with patch.object(p.subprocess,'run',return_value=Mock(returncode=1)):
            with self.assertRaises(ValueError):call('PATCH','/api/v1/admin/partner-users/id',{})
        with patch.object(p.subprocess,'run',side_effect=subprocess.TimeoutExpired('client',90)):
            with self.assertRaises(subprocess.TimeoutExpired):call('PATCH','/api/v1/admin/partner-users/id',{})
        with self.assertRaises(ValueError):p.public_client('http://invalid')

    def test_cli_requires_root_confirmation_and_suppresses_failure_details(self):
        args=['personas','--identities','/root/personas.json','--state','/root/state','--origin','https://192.0.2.1:8443']
        with patch('sys.argv',args),patch.object(p.os,'geteuid',return_value=0):
            with self.assertRaises(ValueError):p.main()
        with patch('sys.argv',args+['--yes']),patch.object(p.os,'geteuid',return_value=1000):
            with self.assertRaises(ValueError):p.main()
        with patch('sys.argv',args+['--yes']),patch.object(p.os,'geteuid',return_value=0),patch.object(p.population.private,'read',return_value=self.identities),patch.object(p,'apply',return_value={'ready':True}) as apply,contextlib.redirect_stdout(io.StringIO()) as output:
            p.main();self.assertIn('"ready": true',output.getvalue());apply.assert_called_once()
        with patch('sys.argv',args),patch('os.geteuid',return_value=1000):
            with self.assertRaises(SystemExit) as error:runpy.run_path(str(Path(p.__file__)),run_name='__main__')
            self.assertIn('No credential diagnostics',str(error.exception))

if __name__=='__main__':unittest.main()
