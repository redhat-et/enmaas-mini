#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('report',Path(__file__).with_name('report.py'))
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)

class Report(unittest.TestCase):
    def test_units_do_not_confuse_decimal_and_binary_memory(self):
        self.assertEqual(report.memory_bytes('2MB'),2000000)
        self.assertEqual(report.memory_bytes('2 MiB'),2097152)
        for invalid in ('NaNMB','-2MB','2PB','2'):
            with self.assertRaises(ValueError):report.memory_bytes(invalid)

    def test_reject_failed_and_nonfinite_samples(self):
        with self.assertRaises(ValueError):report.resources([{'utc':'2026-01-01T00:00:00Z','containers':{'error':'failed'}}])
        with self.assertRaises(ValueError):report.number('nan')
        with self.assertRaises(ValueError):report.stamp('2026-01-01T00:00:00')

    def test_duration_units(self):
        self.assertAlmostEqual(report.cpu_seconds('1m2.5s'),62.5)
        self.assertAlmostEqual(report.cpu_seconds('250ms'),.25)
        with self.assertRaises(ValueError):report.cpu_seconds('NaN')

    def test_normalizes_names_and_keeps_multicore_cpu(self):
        records=[{'utc':f'2026-01-01T00:00:{i:02}Z','containers':[
            {'name':'enmaas-local-gateway-sso','id':'id1','cpu_time':f'{i*2}s','mem_usage':'2 MiB / 10 MiB'}]} for i in (0,10)]
        values=report.resources(records)['gateway']
        self.assertEqual(values[1][1:],(200,2))
        self.assertEqual(values[1][0]-values[0][0],10)

if __name__=='__main__':unittest.main()
