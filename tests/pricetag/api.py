#!/usr/bin/env python3
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import io
import urllib.error
ROOT=Path(__file__).resolve().parents[2]
loader=importlib.machinery.SourceFileLoader('api',str(ROOT/'scripts/pricetag/api'))
spec=importlib.util.spec_from_loader(loader.name,loader);api=importlib.util.module_from_spec(spec);loader.exec_module(api)

class Contracts(unittest.TestCase):
 def test_origin_override_requires_literal_loopback_transport(self):
  self.assertEqual(api.session_origin('https://gateway.example',None),'https://gateway.example')
  for transport in ('https://127.0.0.1:8443','https://[::1]:8443'):
   self.assertEqual(api.session_origin(transport,'https://gateway.example/'),'https://gateway.example')
  for transport in ('https://gateway.example','https://localhost:8443','https://127.0.0.2:8443','https://127.0.0.1.evil.test'):
   with self.assertRaises(ValueError):api.session_origin(transport,'https://gateway.example')
  for origin in ('http://gateway.example','https://user:secret@gateway.example','https://gateway.example/path','https://gateway.example?key=secret','https://gateway.example#fragment','https://gateway.example:bad','https://gateway.example:99999','https://gateway.example\\evil','https://gateway.example\nInjected: value'):
   with self.assertRaises(ValueError):api.session_origin('https://127.0.0.1:8443',origin)
 def test_loopback_requests_keep_transport_and_public_origin_separate(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);key=root/'admin.key';key.write_text('synthetic-test-key');key.chmod(0o600)
   output=root/'response.json';requests=[]
   cookie=mock.Mock(name='cookie');cookie.name='metering_session'
   def open_request(request,timeout):
    requests.append(request)
    if len(requests)==1:
     raise urllib.error.HTTPError(request.full_url,302,'redirect',{},io.BytesIO())
    return io.BytesIO(b'{"users":[]}')
   client=mock.Mock();client.open.side_effect=open_request
   argv=['api','GET','/api/v1/management/users','--url','https://127.0.0.1:8443',
         '--origin','https://gateway.example:8443','--key-file',str(key),'--output',str(output)]
   with mock.patch('sys.argv',argv),mock.patch.object(api.http.cookiejar,'CookieJar',return_value=[cookie]),mock.patch.object(api.urllib.request,'build_opener',return_value=client),mock.patch('builtins.print'):
    api.main()
   self.assertEqual([r.full_url for r in requests],['https://127.0.0.1:8443/login','https://127.0.0.1:8443/api/v1/management/users'])
   self.assertTrue(all(r.get_header('Origin')=='https://gateway.example:8443' for r in requests))
   self.assertEqual(output.read_bytes(),b'{"users":[]}')
   self.assertEqual(output.stat().st_mode&0o777,0o600)
   self.assertIsNone(api.NoRedirect().redirect_request(None,None,None,None,None,None))
 def test_remote_override_fails_before_credentials_or_network(self):
  argv=['api','GET','/api/v1/management/users','--url','https://remote.example',
        '--origin','https://gateway.example','--key-file','missing','--output','unused']
  with mock.patch('sys.argv',argv),mock.patch.object(api,'private') as private,mock.patch.object(api.urllib.request,'build_opener') as opener:
   with self.assertRaises(ValueError):api.main()
   private.assert_not_called();opener.assert_not_called()
 def test_personal_account_routes_are_explicit(self):
  for route in ['/api/v1/me/keys','/api/v1/me/keys/key-id','/api/v1/me/usage/summary','/api/v1/me/usage/models','/api/v1/me/usage/recent']:
   self.assertEqual(api.endpoint('https://example.test',route)[1],route)
  for route in ['/api/v1/me/admin','/api/v1/me/keys-extra','/api/v1/me/usage/summary/other','/api/v1/me/usage/summary?user=other']:
   with self.assertRaises(ValueError):api.endpoint('https://example.test',route)
 def test_rejects_credential_redirect_destinations(self):
  for origin,path in [('http://example.test','/me/keys'),('https://user:password@example.test','/me/keys'),('https://example.test','//other.test/me/keys'),('https://example.test','/api/v1/admin/../events'),('https://example.test','/api/v1/events')]:
   with self.assertRaises(ValueError):api.endpoint(origin,path)
 def test_catalog_query_is_narrowly_allowed(self):
  self.assertEqual(api.endpoint('https://example.test','/api/v1/pricing?used=0')[1],'/api/v1/pricing?used=0')
  for route in ['/api/v1/pricing?used=0&other=1','/me/keys?used=0','/api/v1/pricing?token=secret']:
   with self.assertRaises(ValueError):api.endpoint('https://example.test',route)
 def test_requires_private_owned_input(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'fixture';path.write_text('fixture');path.chmod(0o644)
   with self.assertRaises(ValueError):api.private(path)
   path.chmod(0o600);self.assertEqual(api.private(path),'fixture')
   link=Path(directory)/'link';link.symlink_to(path)
   with self.assertRaises(OSError):api.private(link)

if __name__=='__main__':unittest.main()
