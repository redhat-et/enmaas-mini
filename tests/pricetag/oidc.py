#!/usr/bin/env python3
"""Protected OIDC input validation and gateway route boundaries."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/pricetag'))
import oidc_config
import prepare

class OIDC(unittest.TestCase):
    def config(self,path,**changes):
        document={'issuer_url':'https://issuer.example/realms/lab','client_id':'mini',
                  'client_secret':'synthetic-secret-123456'}
        document.update(changes);path.write_text(json.dumps(document));path.chmod(0o600)
    def test_secure_defaults_and_exact_callback(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'oidc.json';self.config(path)
            env,ca,mapping=oidc_config.load(path,'https://gateway.example:8443')
            self.assertEqual(env['OIDC_REDIRECT_URL'],'https://gateway.example:8443/oauth/callback')
            self.assertEqual(env['OIDC_POST_LOGOUT_REDIRECT_URL'],'https://gateway.example:8443/login')
            for key in ('OIDC_DEVELOPMENT_MODE','OIDC_TEST_LOGIN','OIDC_CLAIMS_DEBUG'):
                self.assertEqual(env[key],'false')
            self.assertEqual(env['OIDC_SCOPES'], 'openid profile email')
            self.assertIsNone(ca)
            self.assertIsNone(mapping)
    def test_private_issuer_mapping_preserves_identity_and_tls(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'oidc.json';self.config(path,issuer_private_ip='10.0.0.11')
            env,ca,mapping=oidc_config.load(path,'https://gateway.example:8443')
            self.assertEqual(mapping,'issuer.example:10.0.0.11')
            self.assertEqual(env['OIDC_ISSUER_URL'],'https://issuer.example/realms/lab')
            self.assertEqual(env['OIDC_DEVELOPMENT_MODE'],'false')
            self.assertNotIn('10.0.0.11',str(env))
            for address in ('127.0.0.1','0.0.0.0','169.254.1.2','8.8.8.8','10.0.0.1\nInjected=yes'):
                self.config(path,issuer_private_ip=address)
                with self.assertRaises(ValueError):oidc_config.load(path,'https://gateway.example:8443')
            self.config(path,issuer_url='https://192.0.2.1/realm',issuer_private_ip='10.0.0.11')
            with self.assertRaises(ValueError):oidc_config.load(path,'https://gateway.example:8443')

    def test_unsafe_values_never_echo_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'oidc.json'
            for change in ({'issuer_url':'http://issuer.example'},
                           {'issuer_url':'https://issuer\\evil.example'},
                           {'issuer_url':'https://user:SECRET@issuer.example'},
                           {'issuer_url':'https://issuer.example/?secret=SECRET'},
                           {'client_secret':'SECRET\nINJECT=true'}, {'debug':True}):
                self.config(path,**change)
                with self.assertRaises(ValueError) as error:oidc_config.load(path,'https://gateway.example:8443')
                self.assertNotIn('SECRET',str(error.exception))
    def test_private_file_and_symlink_contract(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'oidc.json';self.config(path);path.chmod(0o644)
            with self.assertRaises(ValueError):oidc_config.load(path,'https://gateway.example:8443')
            path.chmod(0o600);link=Path(d)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):oidc_config.load(link,'https://gateway.example:8443')
    def test_invalid_encoding_and_oversized_input_have_generic_errors(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'oidc.json'
            for content in (b'SECRET\xff', b'SECRET' * 12000):
                path.write_bytes(content);path.chmod(0o600)
                with self.assertRaises(ValueError) as error:
                    oidc_config.load(path,'https://gateway.example:8443')
                self.assertNotIn('SECRET',str(error.exception))

    def test_gateway_has_exact_get_routes_only(self):
        original,models=prepare.mock_inputs()
        config=prepare.render(original,models)
        edge=next(c for c in config['filter_chains'] if c['name']=='edge')
        allow=next(f for f in edge['filters'] if f.get('status')==404)['conditions']
        for path in ('/oauth/login','/oauth/callback'):
            matches=[r['unless'] for r in allow if r['unless'].get('path')==path]
            self.assertEqual(matches,[{'path':path,'methods':['GET']}])
        self.assertFalse(any(r['unless'].get('path_prefix')=='/oauth/' for r in allow))
        for item in allow:
            rule=item['unless']
            self.assertNotEqual(rule.get('path'),'/api/v1/users')
            self.assertFalse('/api/v1/users/'.startswith(rule['path_prefix']) if 'path_prefix' in rule else False)

if __name__=='__main__':unittest.main()
