"""Continuous monitoring survives outages without publishing fabricated samples."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('local_host_metrics', (Path(__file__).resolve().parents[1] / 'local-host-metrics.py'))
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitoringRecovery(unittest.TestCase):
    def inspection(self, owner=None):
        return SimpleNamespace(stdout=json.dumps([{'Labels': {
            'io.enmaas.test-lab': owner or monitor.signals.lab.INSTANCE}}]))

    def test_continuous_sampling_recovers_without_exposing_error_details(self):
        output = io.StringIO()
        with patch('sys.argv', ['monitor', '--continuous']), \
             patch.object(monitor.subprocess, 'run', return_value=self.inspection()), \
             patch.object(monitor, 'collect', side_effect=[RuntimeError('private endpoint and credential'), [], KeyboardInterrupt]) as collect, \
             patch.object(monitor.time, 'sleep'), contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                monitor.main()
        self.assertEqual(collect.call_count, 3)
        self.assertIn('Host sample unavailable: RuntimeError', output.getvalue())
        self.assertIn('Aggregate host sample 2 delivered', output.getvalue())
        self.assertNotIn('private endpoint', output.getvalue())

    def test_bounded_sampling_fails_instead_of_reporting_success(self):
        with patch('sys.argv', ['monitor', '--samples', '2']), \
             patch.object(monitor.subprocess, 'run', return_value=self.inspection()), \
             patch.object(monitor, 'collect', side_effect=RuntimeError('source unavailable')):
            with self.assertRaises(RuntimeError):
                monitor.main()

    def test_foreign_fixture_is_never_sampled(self):
        with patch('sys.argv', ['monitor', '--continuous']), \
             patch.object(monitor.subprocess, 'run', return_value=self.inspection('foreign')), \
             patch.object(monitor, 'collect') as collect:
            with self.assertRaisesRegex(ValueError, 'Unowned'):
                monitor.main()
            collect.assert_not_called()
