"""Selected key export requires matching private receipts and current API state."""
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock,patch
import contextlib
import io
import runpy

spec=importlib.util.spec_from_file_location('population_key',(Path(__file__).resolve().parents[1] / 'population-key.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class SelectedKey(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.identity=str(uuid.uuid4());self.login='perf-'+'a'*16+'-00001@example.test'
        self.document={'version':1,'population':'perf-'+'a'*16,'profiles':[{'user_id':self.identity,'tags':{'email':self.login,'first_name':'Synthetic','last_name':'User'}}]}
        self.save('import.json',{'population':self.document['population'],'sha256':hashlib.sha256(json.dumps(self.document,sort_keys=True,separators=(',',':')).encode()).hexdigest()})
        self.save(self.identity+'.json',{'state':'applied','user_id':self.identity})
        self.raw=b'synthetic-not-valid-key\n';self.key=self.root/(self.identity+'.key');self.key.write_bytes(self.raw);self.key.chmod(0o600)
        self.expiry=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.receipt={'state':'configured','user_id':self.identity,'key_id':'key-id','expires_at':self.expiry,'key_sha256':hashlib.sha256(self.raw).hexdigest()}
        self.save(self.identity+'.access.json',self.receipt)
        self.identity_read=Mock(return_value={'user_id':self.identity,'active':True})
        self.key_list=Mock(return_value=[{'id':'key-id','status':'active','expirationDate':self.expiry}])
    def save(self,name,value):p.population.state.save(self.root,name,value)
    def select(self,index=1):return p.select(self.document,self.root,index,self.identity_read,self.key_list)
    def test_selected_metadata_contains_no_secret(self):
        metadata,raw=self.select();self.assertEqual(raw,self.raw)
        self.assertEqual(metadata['login'],self.login);self.assertEqual(metadata['key_file'],str(self.key))
        self.assertNotIn(self.raw.decode().strip(),json.dumps(metadata));self.key_list.assert_called_once()
    def test_index_binding_and_retired_identity_refused(self):
        for index in (True,0,2):
            with self.assertRaises(ValueError):self.select(index)
        self.save('import.json',{})
        with self.assertRaises(ValueError):self.select()
        self.assertFalse(self.identity_read.called)
    def test_unconfirmed_identity_or_key_refused(self):
        self.save(self.identity+'.json',{'state':'retired','user_id':self.identity})
        with self.assertRaises(ValueError):self.select()
        self.save(self.identity+'.json',{'state':'applied','user_id':self.identity})
        for receipt in ({},None,dict(self.receipt,state='configuring')):
            self.save(self.identity+'.access.json',receipt)
            with self.assertRaises(ValueError):self.select()
        self.assertFalse(self.identity_read.called)
    def test_expired_or_timezone_free_key_refused(self):
        for expiry in ('2000-01-01T00:00:00+00:00','2099-01-01T00:00:00'):
            self.save(self.identity+'.access.json',dict(self.receipt,expires_at=expiry))
            with self.assertRaises(ValueError):self.select()
    def test_bad_permissions_symlink_and_changed_contents_refused(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):self.select()
        self.key.chmod(0o600);self.key.write_bytes(b'other-key\n')
        with self.assertRaises(ValueError):self.select()
        self.key.unlink();self.key.symlink_to(self.root/'import.json')
        with self.assertRaises(OSError):self.select()
        self.assertFalse(self.identity_read.called)
    def test_current_identity_and_key_status_checked(self):
        for identity in (None,{'user_id':self.identity,'active':False},{'user_id':str(uuid.uuid4()),'active':True}):
            self.identity_read.return_value=identity
            with self.assertRaises(ValueError):self.select()
        self.identity_read.return_value={'user_id':self.identity,'active':True}
        key={'id':'key-id','status':'active','expirationDate':self.expiry}
        for keys in (None,[],[key,key],[dict(key,status='revoked')],[dict(key,status='expired')],[dict(key,expirationDate='different')]):
            self.key_list.return_value=keys
            with self.assertRaises(ValueError):self.select()
    def test_cli_default_metadata_and_explicit_stdout(self):
        args=['population-key','--index','1']
        with patch('sys.argv',args),patch.object(p.os,'geteuid',return_value=0),patch.object(p.population.private,'read',return_value=self.document),patch.object(p,'select',return_value=({'user_id':self.identity},self.raw)) as select,contextlib.redirect_stdout(io.StringIO()) as output:
            p.main();self.assertNotIn('synthetic',output.getvalue())
            with patch.object(p.population.oidc,'partner_request',return_value={}):select.call_args.args[3]({})
        out=Mock();out.buffer=io.BytesIO()
        with patch('sys.argv',args+['--stdout']),patch.object(p.os,'geteuid',return_value=0),patch.object(p.population.private,'read',return_value=self.document),patch.object(p,'select',return_value=({},self.raw)),patch('sys.stdout',out):p.main()
        self.assertEqual(out.buffer.getvalue(),self.raw)
        with patch('sys.argv',args),patch('os.geteuid',return_value=1000):
            with self.assertRaises(ValueError):p.main()
            with self.assertRaises(SystemExit) as error:runpy.run_path(p.__file__,run_name='__main__')
            self.assertIn('No key printed',str(error.exception))

if __name__=='__main__':unittest.main()
