#!/usr/bin/env python3
"""Protected OIDC input validation and gateway route boundaries."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
            fifo = Path(d)/'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):oidc_config.load(fifo,'https://gateway.example:8443')
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

    def test_prepare_writes_private_oidc_settings_and_only_leaf_tls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config, admin = root/'config', root/'admin'
            oidc = root/'oidc.json'
            self.config(oidc, issuer_private_ip='10.0.0.11')
            validated_oidc = oidc_config.load(oidc, 'https://gateway.example:8443')
            prices = root/'prices.json'
            prices.write_text('{}')
            # Certificate validation is exercised by tls-bundle.py; here verify
            # deployment outputs, permissions and absence of a CA signing key.
            files = {}
            for name in ('server.pem', 'server.key', 'ca.pem'):
                files[name] = root/name
                files[name].write_text('synthetic ' + name)
            args = ['prepare.py', '--hostname', 'gateway.example', '--mock-provider',
                    '--price-sources', str(prices), '--directory', str(config),
                    '--admin-directory', str(admin), '--oidc-config', str(oidc),
                    '--tls-bundle', str(root)]
            for flag in ('--metering-image', '--maas-image', '--praxis-image'):
                args += [flag, 'sha256:' + 'a'*64]
            output = io.StringIO()
            old_umask = os.umask(0o077)
            try:
                with patch.object(sys, 'argv', args), patch.object(prepare.os, 'geteuid', return_value=0), \
                     patch.object(prepare.pwd, 'getpwnam', return_value=SimpleNamespace(pw_gid=os.getgid())), \
                     patch.object(prepare.os, 'chown'), patch.object(prepare, 'validate_tls_bundle', return_value=files), \
                     patch.object(prepare, 'load_oidc', return_value=validated_oidc), \
                     patch.object(prepare, 'praxis_image_source',
                                  return_value='https://github.com/redhat-et/praxis-ai'), \
                     contextlib.redirect_stdout(output):
                    prepare.main()
            finally:
                os.umask(old_umask)
            env = (config/'metering.env').read_text()
            self.assertIn('OIDC_ENABLED=true\n', env)
            # The praxis-ai health/metrics listener is rendered alongside the
            # admin API port; older images get no metrics_address at all.
            gateway = json.loads((config/'gateway/gateway.json').read_text())
            self.assertEqual(gateway['admin'], {'address': '127.0.0.1:9901',
                                                'metrics_address': '127.0.0.1:9902'})
            # praxis-ai images additionally route on the body model only, so
            # a client-forged X-Gateway-Model cannot select the upstream.
            routes = [f for chain in gateway['filter_chains'] for f in chain['filters']
                      if f['filter'] == 'intelligent_route']
            self.assertTrue(routes)
            self.assertTrue(all(f['body_authoritative'] is True for f in routes))
            self.assertIn('OIDC_REDIRECT_URL=https://gateway.example:8443/oauth/callback\n', env)
            partner_secret = (config/'partner-secret').read_text().strip()
            self.assertIn('USER_MANAGEMENT_API_SECRET=' + partner_secret + '\n', env)
            self.assertNotEqual(partner_secret, (config/'management-secret').read_text().strip())
            self.assertEqual((config/'partner-secret').stat().st_mode & 0o777, 0o600)
            self.assertEqual((config/'metering.env').stat().st_mode & 0o777, 0o640)
            self.assertEqual((admin/'tls/server-key.pem').stat().st_mode & 0o777, 0o600)
            unit = (config/'quadlets/pricetag-metering.container').read_text()
            self.assertIn('AddHost=issuer.example:10.0.0.11\n', unit)
            self.assertIn('PublishPort=127.0.0.1:18087:8080\n', unit)
            self.assertFalse(list(config.rglob('ca-key.pem')) + list(admin.rglob('ca-key.pem')))
            for secret in ('synthetic-secret-123456', partner_secret):
                self.assertNotIn(secret, output.getvalue())

if __name__=='__main__':unittest.main()
