#!/usr/bin/env python3
"""Host sampling exposes aggregates, never process arguments or filesystem paths."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('host_metrics',Path(__file__).with_name('host-metrics.py'))
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)

class Metrics(unittest.TestCase):
    def fixture(self,root):
        (root/'stat').write_text('cpu  100 20 30 400 5 6 7 8 90 10\nbtime 1000\n')
        (root/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 600 kB\nCached: 200 kB\n')
        (root/'loadavg').write_text('0.25 0.50 1.00 1/100 1234\n')

    def test_aggregate_cpu_memory_and_disk_not_process_data(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            with patch.object(host.os,'sysconf',return_value=100),patch.object(host.os,'statvfs',return_value=SimpleNamespace(f_blocks=100,f_bfree=40,f_bavail=35,f_frsize=4096)):
                payload=host.sample(root,now_ns=2000_000_000_000)
            metrics=payload['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
            by_name={m['name']:m for m in metrics}
            cpu=by_name['enmaas_host_cpu_seconds']['sum']['dataPoints']
            self.assertEqual(len(cpu),8) # guest counters already included in user/nice
            self.assertEqual(cpu[0]['asDouble'],1)
            memory=by_name['enmaas_host_memory_bytes']['gauge']['dataPoints']
            self.assertEqual([p['asDouble'] for p in memory],[1024000,614400,409600,204800])
            self.assertNotIn('1234',str(payload))
            self.assertNotIn('/var',str(payload))
            self.assertNotIn('process',str(payload))

    def test_invalid_kernel_data_and_endpoint_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            (root/'stat').write_text('cpu -1 0 0 0 0 0 0 0\nbtime 1000\n')
            with self.assertRaises(ValueError):host.sample(root)
        for address in ('https://example.test','127.0.0.1','0.0.0.0','169.254.169.254','10.0.0.2:443'):
            with self.assertRaises(ValueError):host.endpoint(address)
        self.assertEqual(host.endpoint('10.0.0.2'),'https://10.0.0.2:24443/v1/metrics')

if __name__=='__main__':unittest.main()
