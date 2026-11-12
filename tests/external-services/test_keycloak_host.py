#!/usr/bin/env python3
"""RHEL issuer units and public route boundaries; no host mutations."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import json

spec=importlib.util.spec_from_file_location('keycloak_host',Path(__file__).with_name('keycloak-host.py'))
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)

class Host(unittest.TestCase):
    def test_dns_issuer_and_private_gateway_mapping_keep_exact_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            host.configure(root,'192.0.2.10','192.0.2.11','192.0.2.12',
                           issuer_name='identity.lab.example.com',issuer_private_ip='10.0.0.10')
            oidc=json.loads((root/'gateway-oidc.json').read_text())
            self.assertEqual(oidc['issuer_url'],'https://identity.lab.example.com:8443/realms/enmaas-lab')
            self.assertEqual(oidc['issuer_private_ip'],'10.0.0.10')
            self.assertIn('hostname=https://identity.lab.example.com:8443', (root/'keycloak.conf').read_text())
            self.assertIn('hostname-admin=https://localhost:18443', (root/'keycloak.conf').read_text())
            realm=json.loads((root/'realm.json').read_text())
            self.assertEqual(realm['clients'][1]['redirectUris'],['https://192.0.2.11:8443/oauth/callback'])

    def test_private_issuer_requires_complete_valid_pair_before_writes(self):
        for options in ({'issuer_name':'identity.lab.example.com'},
                        {'issuer_private_ip':'10.0.0.10'},
                        {'issuer_name':'identity.lab.example.com','issuer_private_ip':'8.8.8.8'}):
            with tempfile.TemporaryDirectory() as d:
                root=Path(d)
                with self.assertRaises(ValueError):
                    host.configure(root,'192.0.2.10','192.0.2.11','192.0.2.12',**options)
                self.assertFalse(list(root.iterdir()))

    def test_units_keep_database_private_and_admin_loopback_only(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            host.configure(root,'192.0.2.10','192.0.2.11','192.0.2.12')
            units={p.name:p.read_text() for p in (root/'quadlets').iterdir()}
            self.assertNotIn('PublishPort',units['lab-keycloak-db.container'])
            self.assertIn('PublishPort=127.0.0.1:18443:18443',units['lab-keycloak.container'])
            self.assertIn('PublishPort=8443:8443',units['lab-keycloak-proxy.container'])
            self.assertIn('User=999:999',units['lab-keycloak-db.container'])
            for name,text in units.items():
                if name.endswith('.container'):
                    self.assertIn('NoNewPrivileges=true',text)
                    self.assertIn('DropCapability=all',text)
                    self.assertNotIn('Privileged=true',text)
                    self.assertNotIn('Network=host',text)
            config=json.loads((root/'proxy.json').read_text())
            reject=config['filter_chains'][0]['filters'][0]
            self.assertEqual(reject['status'],404)
            allowed=[c['unless']['path_prefix'] for c in reject['conditions']]
            self.assertEqual(allowed,['/realms/enmaas-lab/','/resources/'])
            realm=json.loads((root/'realm.json').read_text())
            self.assertTrue(realm['bruteForceProtected'])
            self.assertFalse(realm['permanentLockout'])
            self.assertEqual(realm['failureFactor'],5)
            self.assertNotIn('lab-authorization-test', {c['clientId'] for c in realm['clients']})
            self.assertNotIn('authorization_test', json.loads((root/'credentials.json').read_text()))
            self.assertEqual(realm['clients'][1]['redirectUris'],['https://192.0.2.11:8443/oauth/callback'])
            self.assertEqual(realm['clients'][1]['attributes']['post.logout.redirect.uris'],'https://192.0.2.11:8443/login')
            self.assertEqual((root/'credentials.json').stat().st_mode&0o777,0o600)
            secret=root/'observability-client-secret'
            self.assertEqual(secret.read_text(),json.loads((root/'credentials.json').read_text())['oidc'])
            self.assertEqual(secret.stat().st_mode&0o777,0o600)

    def test_invalid_address_and_existing_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(ValueError):host.configure(root,'host\nInjected=yes','192.0.2.11','192.0.2.12')
            host.configure(root,'192.0.2.10','192.0.2.11','192.0.2.12')
            with self.assertRaises(FileExistsError):host.configure(root,'192.0.2.10','192.0.2.11','192.0.2.12')

if __name__=='__main__':unittest.main()
