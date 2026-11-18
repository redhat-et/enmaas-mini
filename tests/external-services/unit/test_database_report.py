import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('report',(Path(__file__).resolve().parents[1] / 'database-report.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class DatabaseReport(unittest.TestCase):
    def test_total_and_mean_rankings_are_distinct(self):
        def row(fingerprint,calls,total):
            return dict(fingerprint=fingerprint,operation='usage-read',calls=calls,total_ms=total,
                mean_ms=total/calls,maximum_since_reset_ms=150,cache_hits=3,disk_reads=1,temporary_blocks=0)
        text=m.render([{'phase':'stress_10','statements':[row('1',100,1000),row('2',1,100)]}])
        self.assertLess(text.index('| `1`'),text.index('| `2`'))
        self.assertIn('`2` (usage-read): 100.000 ms across 1 calls',text)
        self.assertIn('not the stage maximum',text)

    def test_diagnostic_labels_are_fixed_not_freeform_sql(self):
        with self.assertRaises(ValueError):m.render([],{'1':'SELECT private data'})
        with self.assertRaises(ValueError):m.render([],{'secret':'role lookup by login'})
        self.assertIn('Database workload',m.render([],{'1':'role lookup by login'}))

    def test_stage_overview_is_weighted_and_handles_empty_stage(self):
        rows=[dict(fingerprint=str(i),operation='read',calls=c,total_ms=t,
                   mean_ms=t/c,maximum_since_reset_ms=500,cache_hits=1,
                   disk_reads=0,temporary_blocks=0)
              for i,c,t in [(1,100,1000),(2,1,100)]]
        text=m.render([{'phase':'stress_10','statements':rows},
                       {'phase':'stress_50','statements':[]}])
        self.assertIn('| stress_10 | 101 | 1.100 | 10.891 |',text)
        self.assertIn('| stress_50 | 0 | 0.000 | — |',text)
        self.assertIn('not request latency',text)
