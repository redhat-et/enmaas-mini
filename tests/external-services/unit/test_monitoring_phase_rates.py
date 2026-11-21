"""Phase-start samples must not turn one request into a false throughput spike."""
import json
import unittest
import benchmark_monitoring as monitoring

class PhaseRates(unittest.TestCase):
    def test_elapsed_denominators_have_one_second_floor(self):
        rendered=json.dumps(monitoring.all_dashboards())
        self.assertIn('/ clamp_min(enmaas_stress_elapsed_seconds, 1)',rendered)
        self.assertNotIn('/ enmaas_stress_elapsed_seconds',rendered)
        self.assertNotIn('/ (enmaas_stress_elapsed_seconds > 0)',rendered)
        # The first scheduled request can precede the first full elapsed second.
        self.assertEqual(1 / max(0.0000148, 1),1)
