import unittest
from workload_signals import payloads

class Signals(unittest.TestCase):
    def test_generator_signals_do_not_impersonate_gateway_or_contain_user_metadata(self):
        result = payloads(37,now=100)
        self.assertEqual(set(result),{'metrics','logs'})
        text = str(result)
        self.assertIn('enmaas-mock-benchmark',text)
        self.assertNotIn('enmaas-gateway',text)
        self.assertNotIn('user',text)
        self.assertEqual(result['metrics']['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['gauge']['dataPoints'][0]['asInt'],'37')
    def test_counts_are_bounded(self):
        for value in (-1, True, '3', 100000001):
            with self.assertRaises(ValueError): payloads(value)
