"""Query profiling exports no statement text, identities or bind parameters."""
import importlib.util
from pathlib import Path
import unittest
import json
from unittest.mock import patch
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('profile',(Path(__file__).resolve().parents[1] / 'database-profile.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class Profile(unittest.TestCase):
    def row(self,**changes):
        row={'fingerprint':'123','operation':'usage-read','calls':4,'total_ms':20.,'maximum_ms':9.,'rows':10,'cache_hits':20,'disk_reads':1,'temporary_blocks':0}
        return dict(row,**changes)

    def test_allowlist_rejects_text_and_invalid_values(self):
        self.assertEqual(p.validate([self.row()]),[self.row()])
        for changes in ({'query':'SELECT secret'},{'username':'alice'},{'calls':-1},{'total_ms':float('nan')},{'operation':'private-key'},{'fingerprint':'secret'}):
            with self.assertRaises(ValueError):p.validate([self.row(**changes)])
        with self.assertRaises(ValueError):p.validate([self.row(),self.row()])

    def test_phase_delta_and_lifetime_max_are_distinct(self):
        rows=p.delta([self.row()],[self.row(calls=6,total_ms=50.,maximum_ms=18.,rows=12)])
        self.assertEqual(rows[0]['calls'],2)
        self.assertEqual(rows[0]['total_ms'],30)
        self.assertEqual(rows[0]['mean_ms'],15)
        self.assertEqual(rows[0]['maximum_since_reset_ms'],18)
        self.assertNotIn('p95',rows[0])
        with self.assertRaises(ValueError):p.delta([self.row()],[self.row(calls=1)])

    def test_eviction_and_reclassification_reject_interval(self):
        with self.assertRaises(ValueError):p.delta([self.row()],[])
        with self.assertRaises(ValueError):p.delta([self.row()],[self.row(operation="write")])

    def test_collect_accepts_multiline_aggregate_without_sql_command_tags(self):
        rows=[self.row(),self.row(fingerprint='456')]
        with patch.object(p.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(rows,indent=2))) as run:
            self.assertEqual(p.collect('enmaas-local'),rows)
            self.assertIn('-q',run.call_args.args[0])
            self.assertIn('ON_ERROR_STOP=1',run.call_args.args[0])

    def test_query_text_never_selected_into_output(self):
        self.assertIn('pg_stat_statements',p.QUERY)
        self.assertNotIn('SELECT *',p.QUERY)
        self.assertNotIn('query AS',p.QUERY)
        self.assertIn('LIMIT 1001',p.QUERY)

if __name__=='__main__':unittest.main()

class SnapshotIntegrity(unittest.TestCase):
    def test_reset_or_eviction_invalidates_comparison(self):
        a={'stats_reset':'2026-11-15T00:00:00Z','dealloc':0,'rows':[]}
        self.assertEqual(p.compare(a,a),[])
        for b in (dict(a,dealloc=1),dict(a,stats_reset='2026-11-16T00:00:00Z')):
            with self.assertRaises(ValueError):p.compare(a,b)
