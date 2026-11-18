"""CPU graphs use interval counters, not Podman's lifetime average."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('population_report',(Path(__file__).resolve().parents[1] / 'population-report.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)


class Report(unittest.TestCase):
    def row(self,at=100,counter='2s',identity='one',name='enmaas-issuer-path-test',memory='512MiB / 1GiB'):
        return {'time':at,'samples':[{'id':identity,'name':name,'cpu_time':counter,'mem_usage':memory,'cpu_percent':'999%'}]}

    def test_interval_cpu_and_memory_units(self):
        result=p.series([self.row(),self.row(102,'5s')])
        self.assertIsNone(result[0]['cpu_percent'])
        self.assertEqual(result[1]['cpu_percent'],150)
        self.assertEqual(result[1]['memory_mib'],512)
        self.assertEqual(result[1]['limit_mib'],1024)

    def test_reject_incomplete_or_incomparable_samples(self):
        for rows in ([],[self.row()],[dict(self.row(),samples=[])],
                     [self.row(),self.row(100,'3s')],[self.row(),self.row(102,'1s')],
                     [self.row(),self.row(102,'3s',identity='two')],
                     [self.row(name='real-gateway')],[self.row(name='enmaas-issuer-path-test-proxy')],
                     [self.row(memory='2GiB / 1GiB')],[self.row(memory='0B / 0B')]):
            with self.assertRaises(ValueError):p.series(rows)


if __name__=='__main__':unittest.main()
