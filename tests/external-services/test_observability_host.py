#!/usr/bin/env python3
"""Dedicated observation host publishing and client-secret boundaries."""
import importlib.util
from pathlib import Path
import tempfile
import json
import unittest

spec=importlib.util.spec_from_file_location('obs',Path(__file__).with_name('observability-host.py'))
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)

class Host(unittest.TestCase):
    def test_private_issuer_resolution_is_pinned_in_pod_and_preserves_tls(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            host.configure(root,'192.0.2.10','192.0.2.12','synthetic-secret-123456','10.0.0.12',
                           issuer_name='identity.lab.example.com',issuer_private_ip='10.0.0.10')
            pod=(root/'quadlets/lab-observability.pod').read_text()
            self.assertIn('AddHost=identity.lab.example.com:10.0.0.10',pod)
            auth=(root/'oauth.cfg').read_text()
            self.assertIn('https://identity.lab.example.com:8443/realms/enmaas-lab',auth)
            self.assertIn('provider_ca_files = ["/lab/issuer-ca.pem"]',auth)
            self.assertNotIn('ssl_insecure_skip_verify = true',auth)
            self.assertIn('allowed_groups = ["/observability-admin"]',auth)

    def test_only_browser_and_mtls_ports_are_published(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);host.configure(root,'192.0.2.10','192.0.2.12','synthetic-secret-123456','10.0.0.12')
            pod=(root/'quadlets/lab-observability.pod').read_text()
            self.assertIn('PublishPort=8443:8443',pod)
            self.assertIn('PublishPort=10.0.0.12:24443:24443',pod)
            self.assertNotIn('PublishPort=24443:24443',pod)
            self.assertEqual(pod.count('PublishPort='),2)
            for unit in (root/'quadlets').glob('*.container'):
                text=unit.read_text()
                self.assertIn('Pod=lab-observability.pod',text)
                self.assertNotIn('PublishPort=',text)
                self.assertIn('DropCapability=all',text)
                self.assertNotIn('synthetic-secret',text)
            auth=(root/'oauth.cfg').read_text()
            self.assertIn('https://192.0.2.10:8443/realms/enmaas-lab',auth)
            self.assertIn('https://192.0.2.12:8443/oauth2/callback',auth)
            self.assertIn('provider_ca_files = ["/lab/issuer-ca.pem"]',auth)
            self.assertIn('allowed_groups = ["/observability-admin"]',auth)
            collector=(root/'collector.yaml').read_text()
            self.assertIn('client_ca_file: /lab/telemetry-ca.pem',collector)
            self.assertIn('keep_keys',collector)
            self.assertIn('set(body, "gateway event")',collector)
            self.assertFalse((root/'credentials.json').exists())

    def test_all_dashboard_assets_are_generated_and_mounted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);host.configure(root,'192.0.2.10','192.0.2.12','synthetic-secret-123456','10.0.0.12')
            unit=(root/'quadlets/lab-obs-perses.container').read_text()
            for asset in host.lab.PERSES_ASSETS:
                self.assertTrue((root/asset).is_file(),asset)
                json.loads((root/asset).read_text())
                self.assertIn('target=/etc/perses/lab/'+asset,unit)
            datasource=json.loads((root/'tempo-datasource.json').read_text())
            self.assertEqual(datasource['spec']['plugin']['spec']['proxy']['spec']['url'],'http://127.0.0.1:3200')
            self.assertNotIn('directUrl',str(datasource))
        installer=Path(__file__).with_name('install-observability').read_text()
        self.assertIn('lab.PERSES_ASSETS',installer)

    def test_private_binding_rejects_public_loopback_and_wildcard(self):
        for address in ('0.0.0.0', '127.0.0.1', '169.254.1.1', '8.8.8.8', '::1', '192.0.2.12'):
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    host.configure(Path(d),'192.0.2.10','192.0.2.12',
                                   'synthetic-secret-123456',address)
                self.assertFalse(list(Path(d).iterdir()))

    def test_installer_requires_binding_to_assigned_private_address(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            host.configure(root,'192.0.2.10','192.0.2.12','synthetic-secret-123456','10.0.0.12')
            interfaces = [{'addr_info': [{'family': 'inet', 'local': '10.0.0.12'}]}]
            self.assertEqual(host.validate_host_binding(root, interfaces), '10.0.0.12')
            with self.assertRaises(ValueError):
                host.validate_host_binding(root, [])
            pod = root/'quadlets/lab-observability.pod'
            pod.write_text(pod.read_text().replace('10.0.0.12:24443', '0.0.0.0:24443'))
            with self.assertRaises(ValueError):
                host.validate_host_binding(root, interfaces)

    def test_reject_secret_injection(self):
        with tempfile.TemporaryDirectory() as d:
            for secret in ('short','SECRET\ninvalid=true','"quoted-secret-123456"'):
                with self.assertRaises(ValueError):host.configure(Path(d),'192.0.2.10','192.0.2.12',secret,'10.0.0.12')
            self.assertFalse(list(Path(d).iterdir()))

if __name__=='__main__':unittest.main()
