"""Local-only issuer transport validates TLS/token/location and does not replay POST."""
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
import urllib.error

spec=importlib.util.spec_from_file_location('populate_keycloak',(Path(__file__).resolve().parents[1] / 'populate-keycloak.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
UID='8beb9bf1-7cb8-47fa-ac79-a72947000376'


class Transport(unittest.TestCase):
    def client(self):
        with patch.object(p.ssl,'create_default_context'),patch.object(p.urllib.request,'build_opener'):
            return p.Issuer('/ca',{'password':'credential-sentinel'})

    def test_request_boundary_and_redaction(self):
        c=self.client();reply=Mock(status=200,headers={});reply.read.return_value=b'{"ok":true}'
        c.opener.open.return_value.__enter__.return_value=reply
        self.assertEqual(c.request('/test')[0],{'ok':True})
        c.request('/test','POST',{'password':'sentinel'},form=True,token='token')
        request=c.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://localhost:18443/test')
        self.assertEqual(request.data,b'password=sentinel')
        self.assertEqual(request.headers['Authorization'],'Bearer token')
        for status,raw in [(500,b'credential-sentinel'),(200,b'x'*65537),(200,b'bad-json')]:
            reply.status=status;reply.read.return_value=raw
            with self.assertRaises(ValueError) as caught:c.request('/test','POST',{})
            self.assertNotIn('credential-sentinel',str(caught.exception))
        for error in [OSError('credential-sentinel'),urllib.error.HTTPError('https://localhost',409,'credential-sentinel',{},None)]:
            c.opener.open.reset_mock();c.opener.open.side_effect=error
            with self.assertRaises(ValueError):c.request('/test','POST',{})
            self.assertEqual(c.opener.open.call_count,1)
        with self.assertRaises(ValueError):p.NoRedirect().redirect_request()
        with self.assertRaises(ValueError):p.Issuer('/ca',{},origin='https://external.example')

    def test_token_refresh_and_invalid_replies(self):
        c=self.client()
        with patch.object(c,'request',return_value=({'access_token':'first','expires_in':60},None)) as request,patch.object(p.time,'monotonic',return_value=0):
            self.assertEqual(c.access_token(),'first');c.access_token();self.assertEqual(request.call_count,1)
        with patch.object(c,'request',return_value=({'access_token':'second','expires_in':60},None)),patch.object(p.time,'monotonic',return_value=56):
            self.assertEqual(c.access_token(),'second')
        for result in ([],{}, {'access_token':'x\ny','expires_in':60},{'access_token':'token','expires_in':True}, {'access_token':'token','expires_in':5}):
            c.token=None
            with patch.object(c,'request',return_value=(result,None)),self.assertRaises(ValueError):c.access_token()

    def test_only_canonical_same_origin_user_locations_are_accepted(self):
        c=self.client()
        with patch.object(c,'access_token',return_value='token'),patch.object(c,'request',return_value=(None,c.origin+'/admin/realms/enmaas-lab/users/'+UID)):
            self.assertEqual(c.create_user({'username':'test'}),UID)
        for location in (None,'https://other.example/'+UID,c.origin+'/admin/realms/enmaas-lab/users/'+UID.upper()):
            with patch.object(c,'access_token',return_value='token'),patch.object(c,'request',return_value=(None,location)),self.assertRaises(ValueError):c.create_user({})
        with patch.object(c,'access_token',return_value='token'),patch.object(c,'request',return_value=({'id':UID},None)):
            self.assertEqual(c.inspect_user(UID),{'id':UID})
        with self.assertRaises(ValueError):c.inspect_user(UID.upper())

    def test_cli_requires_root_confirmation_and_preserves_existing_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';args=[str(Path(p.__file__)),'--state',str(root),'--count','2']
            with patch('sys.argv',args),self.assertRaises(ValueError):p.main()
            for extra in (['--yes','--count','0'],['--yes']):
                with patch('sys.argv',args+extra),patch.object(p.os,'geteuid',return_value=0),patch.object(p.private,'read',return_value={}),self.assertRaises(ValueError):p.main()
            with patch.object(p.os,'geteuid',return_value=0),patch.object(p.private,'read',return_value={'bootstrap_admin':'credential-sentinel'}),patch.object(p,'Issuer') as issuer,patch.object(p.population,'run',return_value={'issuer_users_verified':2,'pricetag_provisioned':False}):
                for _ in range(2):
                    with patch('sys.argv',args+['--yes']),contextlib.redirect_stdout(io.StringIO()) as out:p.main()
                    self.assertNotIn('credential-sentinel',out.getvalue())
                export=Path(tmp)/'profiles.json'
                with patch('sys.argv',args+['--yes','--export',str(export)]),patch.object(p.population,'export_profiles',return_value={'profiles':[]}),contextlib.redirect_stdout(io.StringIO()):
                    p.main()
                self.assertEqual(json.loads(export.read_text()),{'profiles':[]})
                self.assertEqual(export.stat().st_mode&0o777,0o600)
                with patch('sys.argv',args+['--yes','--export',str(export)]),patch.object(p.population,'export_profiles',return_value={'profiles':[]}),self.assertRaises(FileExistsError):
                    p.main()
                with patch('sys.argv',args+['--yes','--count','3']),self.assertRaises(ValueError):p.main()
                issuer.return_value.access_token.side_effect=ValueError('no auth')
                with patch('sys.argv',args+['--yes']),self.assertRaises(ValueError):p.main()
            result=subprocess.run([sys.executable,*args],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,'')


if __name__=='__main__':unittest.main()
