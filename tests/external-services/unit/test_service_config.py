"""Shared service inputs must remain private and independent of load generation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import yaml

HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('service_inputs',HERE/'service_config.py')
config=importlib.util.module_from_spec(spec);spec.loader.exec_module(config)


class ServiceConfig(unittest.TestCase):
    def test_invalid_secret_cannot_create_outputs(self):
        for secret in ('short','x'*4097,'x'*20+'\n','x'*20+'"',None):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'inputs'
                with self.assertRaises(ValueError):config.configure(root,secret)
                self.assertFalse(root.exists())

    def test_private_inputs_explicit_client_and_no_credential_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config.configure(root,'isolated-observability-client')
            self.assertEqual(root.stat().st_mode&0o777,0o700)
            for path in root.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
            auth=(root/'oauth.cfg').read_text()
            self.assertIn('client_secret = "isolated-observability-client"',auth)
            self.assertIn('allowed_groups = ["/observability-admin"]',auth)
            self.assertIn('code_challenge_method = "S256"',auth)
            self.assertIn('reverse_proxy = false',auth)
            self.assertFalse((root/'credentials.json').exists())
            with self.assertRaises(FileExistsError):config.configure(root,'replacement-client-secret')
            self.assertEqual(auth,(root/'oauth.cfg').read_text())

    def test_all_signals_redacted_and_backends_not_remotely_proxied(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config.configure(root,'isolated-observability-client')
            collector=yaml.safe_load((root/'collector.yaml').read_text())
            for pipeline in collector['service']['pipelines'].values():
                self.assertEqual(pipeline['processors'],['memory_limiter','transform','batch'])
            receiver=collector['receivers']['otlp']['protocols']['http']
            self.assertEqual(receiver['max_request_body_size'],4194304)
            self.assertTrue(receiver['tls']['client_ca_file'])
            perses=yaml.safe_load((root/'perses.yaml').read_text())
            self.assertTrue(perses['datasource']['disable_local'])
            self.assertEqual(perses['datasource']['proxy']['allowed_networks'],['127.0.0.1/32'])
            self.assertEqual(yaml.safe_load((root/'monitoring-rules.yaml').read_text()),config.monitoring.rules())
            for name,expected in config.monitoring.all_dashboards().items():
                self.assertEqual(json.loads((root/name).read_text()),expected)
            self.assertNotIn('benchmark-dashboard.json',config.PERSES_ASSETS)
            self.assertEqual(json.loads((root/'dashboard.json').read_text())['metadata']['name'],'local-transport')


if __name__=='__main__':unittest.main()
