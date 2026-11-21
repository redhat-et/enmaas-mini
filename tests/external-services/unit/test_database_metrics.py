import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import json

spec=importlib.util.spec_from_file_location('database_metrics',(Path(__file__).resolve().parents[1] / 'database-metrics.py'))
db=importlib.util.module_from_spec(spec);spec.loader.exec_module(db)

class DatabaseMetrics(unittest.TestCase):
    def test_only_bounded_aggregate_measurements(self):
        values={name:1 for name in db.FIELDS}
        result=db.payload(values,'db-2',now=1000)
        metrics=result['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        self.assertEqual(len(metrics),9)
        for metric in metrics:
            field=metric['name'].removeprefix('enmaas_postgres_')
            data=metric['sum' if field in db.COUNTERS else 'gauge']
            self.assertEqual(data['dataPoints'][0]['attributes'][0]['value']['stringValue'],'db-2')
        for change in ({'size_bytes':-1},{'connections':float('nan')},{'deadlocks':True},{'secret':'no'}):
            with self.assertRaises(ValueError):db.payload(dict(values,**change))
        with self.assertRaises(ValueError):db.payload({})
        with self.assertRaises(ValueError):db.payload(values,'private.example')

    def test_local_query_is_read_only_and_bounded(self):
        values={name:1 for name in db.FIELDS}
        with patch.object(db.subprocess,'run',return_value=SimpleNamespace(stdout='BEGIN\nSET\n'+json.dumps(values)+'\nCOMMIT\n')) as run:
            db.collect('enmaas-test')
            args=run.call_args.args[0]
            self.assertIn('BEGIN READ ONLY;',args[-1])
            self.assertIn("statement_timeout='3s'",args[-1])
            self.assertEqual(run.call_args.kwargs['timeout'],8)
            self.assertNotIn('password',' '.join(args).lower())
        with self.assertRaises(ValueError):db.collect('foreign-container')

if __name__=='__main__':unittest.main()
