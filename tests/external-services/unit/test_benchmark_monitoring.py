"""Benchmark dashboards are opt-in and cannot change production definitions."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]


def load(filename):
    spec = importlib.util.spec_from_file_location(filename, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchmarkMonitoring(unittest.TestCase):
    def test_operational_renderer_remains_unmodified(self):
        operational = load('monitoring.py')
        before = json.dumps(operational.all_dashboards(), sort_keys=True)
        benchmark = load('benchmark_monitoring.py')
        combined = benchmark.all_dashboards()
        self.assertEqual(before, json.dumps(operational.all_dashboards(), sort_keys=True))
        self.assertIn('benchmark-dashboard.json', combined)
        for name, document in operational.all_dashboards().items():
            self.assertEqual(combined[name], document)
        self.assertNotIn('enmaas_stress_', before)

    def test_rules_preserve_operational_group_and_isolate_generator(self):
        operational = load('monitoring.py')
        benchmark = load('benchmark_monitoring.py')
        groups = benchmark.rules()['groups']
        self.assertEqual(groups[:-1], operational.rules()['groups'])
        self.assertEqual(groups[-1]['name'], 'enmaas-benchmark')
        self.assertEqual(len(groups[-1]['rules']), 3)
        for rule in groups[-1]['rules']:
            self.assertEqual(rule['labels']['category'], 'generator')
            self.assertIn('enmaas_stress_sample_unixtime_seconds', rule['expr'])

    def test_local_only_assets_written_and_all_dashboard_links_resolve(self):
        benchmark = load('benchmark_monitoring.py')
        with tempfile.TemporaryDirectory() as temp:
            benchmark.write(Path(temp))
            self.assertTrue((Path(temp) / 'benchmark-dashboard.json').is_file())
            self.assertTrue((Path(temp) / 'monitoring-rules.yaml').is_file())
        documents = benchmark.all_dashboards()
        targets = {doc['metadata']['name'] for doc in documents.values()}
        for panel in documents['benchmark-dashboard.json']['spec']['panels'].values():
            for link in panel['spec'].get('links', []):
                self.assertIn(link['url'].rsplit('/', 1)[-1], targets)


if __name__ == '__main__':
    unittest.main()
