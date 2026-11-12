#!/usr/bin/env python3
"""Mock inference dependencies remain private and finitely scoped."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('gateway_fixture',Path(__file__).with_name('gateway-sso.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class GatewayFixture(unittest.TestCase):
    def test_maas_environment_trusts_only_the_local_pod(self):
        env=fixture.maas_environment()
        self.assertEqual(env['MAAS_TRUSTED_PROXY_CIDRS'],'127.0.0.1/32')
        self.assertEqual(env['API_KEY_MAX_EXPIRATION_DAYS'],'1')
        self.assertEqual(env['DB_CONNECTION_URL_FILE'],'/run/secrets/maas-db-url')
        self.assertEqual(env['METRICS_ADDRESS'],'127.0.0.1:9909')
        self.assertEqual(env['ADDRESS'],'127.0.0.1:8088')

    def test_reconciliation_requires_exact_events_and_token_totals(self):
        subject='lab-probe-'+'a'*32
        with patch.object(fixture,'run',return_value=b'2|4|6|10|2|0.00001600\n'):
            fixture.reconcile_probe(subject)
        with patch.object(fixture,'run',return_value=b'3|6|9|15|3|0.00002400\n'):
            with self.assertRaises(ValueError):fixture.reconcile_probe(subject)
        with patch.object(fixture,'run') as execute:
            with self.assertRaises(ValueError):fixture.reconcile_probe("invalid' subject")
            execute.assert_not_called()

    def test_reconciliation_rejects_missing_or_wrong_recorded_cost(self):
        for row in (b'2|4|6|10|1|0.00000800',b'2|4|6|10|2|0',
                    b'2|4|6|10|2|0.00001700'):
            with self.subTest(row=row), patch.object(fixture,'run',return_value=row), patch.object(fixture.time,'sleep'):
                with self.assertRaises(ValueError):fixture.reconcile_probe('lab-probe-'+'a'*32)

    def test_role_fixture_rejects_arbitrary_role_before_service_calls(self):
        with patch.object(fixture,'run') as run:
            with self.assertRaises(ValueError):fixture.role_action('unreviewed-role')
            run.assert_not_called()

    def test_mock_dependencies_have_no_published_ports_or_host_mounts(self):
        calls=[]
        with patch.object(fixture,'run',side_effect=lambda *args,**kwargs:calls.append(args) or b''):
            fixture.run_mock_dependencies('reviewed-maas-image')
        self.assertEqual(len(calls),2)
        for args in calls:
            self.assertIn('--pod',args)
            self.assertIn('--read-only',args)
            self.assertIn('--cap-drop',args)
            self.assertNotIn('--publish',args)
            self.assertNotIn('-p',args)
            self.assertNotIn('--volume',args)
        self.assertIn('reviewed-maas-image',calls[0])

if __name__=='__main__':unittest.main()
