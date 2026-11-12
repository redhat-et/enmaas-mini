"""Imported-rate verification uses bounded reviewed IDs and exact rounding."""
import importlib.util
from pathlib import Path
from decimal import Decimal
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('profile_probe', Path(__file__).with_name('profile-inference.py'))
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)

class ProfilePricing(unittest.TestCase):
    def test_native_headers_match_gateway_authentication_contract(self):
        self.assertEqual(probe.request_headers('openai', 'test-key'),
            {'Content-Type': 'application/json', 'Authorization': 'Bearer test-key'})
        self.assertEqual(probe.request_headers('anthropic', 'test-key'),
            {'Content-Type': 'application/json', 'x-api-key': 'test-key', 'anthropic-version': '2023-06-01'})
        with self.assertRaises(ValueError): probe.request_headers('unknown', 'test-key')

    def test_missing_price_is_not_silently_treated_as_free(self):
        with patch.object(probe, 'database', return_value=''):
            with self.assertRaises(ValueError): probe.request_cost('model')

    def test_exact_rate_rounding_and_explicit_zero(self):
        for row, expected in [('0|0', '0'), ('0.0000|0.0050', '0.00000002'), ('1|2', '0.000008')]:
            with self.subTest(row=row), patch.object(probe, 'database', return_value=row):
                self.assertEqual(probe.request_cost('model'), Decimal(expected))

    def test_unreviewed_id_cannot_reach_database(self):
        with patch.object(probe, 'database') as query:
            with self.assertRaises(ValueError): probe.request_cost("model'; SELECT")
            query.assert_not_called()

if __name__ == '__main__':
    unittest.main()
