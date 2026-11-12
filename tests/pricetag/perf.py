#!/usr/bin/env python3
"""Load generator regressions without a provider or live credentials."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
loader=importlib.machinery.SourceFileLoader('perf',str(ROOT/'scripts/pricetag/perf'))
spec=importlib.util.spec_from_loader(loader.name,loader)
perf=importlib.util.module_from_spec(spec);loader.exec_module(perf)


class Load(unittest.TestCase):
    def test_mock_cost_snapshot_is_finite_unique_and_rounded_per_event(self):
        args=SimpleNamespace(database_name='metering',database='test-db',service_user=None)
        with patch.object(perf,'ledger_query',return_value='0.333|0.777\n'):
            self.assertEqual(str(perf.mock_request_cost(args,'example/model')),'0.00000300')
        for row in ('','1|2\n1|2','-1|2','NaN|2','Infinity|2'):
            with self.subTest(row=row),patch.object(perf,'ledger_query',return_value=row):
                with self.assertRaises(ValueError):perf.mock_request_cost(args,'example/model')
        with patch.object(perf,'ledger_query') as query:
            with self.assertRaises(ValueError):perf.mock_request_cost(args,"model' OR 1=1")
            query.assert_not_called()

    def test_usd_reconciliation_checks_exact_sum_and_nonnull_events(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'report.json'
            report.write_text(json.dumps({'schema':'enmaas-mock-counts-v1',
                'completed_mock_requests':{'perf-one':2},'expected_usd_per_request':'0.00000800'}))
            args=SimpleNamespace(report=report,database_name='metering',database='test-db',service_user=None)
            for row,expected in (('perf-one|2|4|6|10|2|0.00001600',0),
                                 ('perf-one|2|4|6|10|2|0.00001601',1),
                                 ('perf-one|2|4|6|10|1|0.00001600',1)):
                with self.subTest(row=row),patch.object(perf.subprocess,'check_output',return_value=row),patch.object(perf.time,'sleep'):
                    self.assertEqual(perf.verify(args),expected)
            for value in ('NaN','Infinity','-1',0.1,'0.000000001'):
                report.write_text(json.dumps({'schema':'enmaas-mock-counts-v1',
                    'completed_mock_requests':{'perf-one':2},'expected_usd_per_request':value}))
                with self.subTest(value=value),patch.object(perf,'ledger_query') as query:
                    with self.assertRaises(ValueError):perf.verify(args)
                    query.assert_not_called()
            report.write_text(json.dumps({'schema':'enmaas-mock-counts-v1',
                'completed_mock_requests':{'perf-one':2},'expected_usd_per_request':'0.00000000'}))
            with patch.object(perf,'ledger_query',return_value='perf-one|2|4|6|10|2|0'):
                self.assertEqual(perf.verify(args),0)

    def test_verification_targets_the_upgrade_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'report.json'
            report.write_text(json.dumps({'phases':[{'samples':[{'subject':'perf-one','ok':True}]}]}))
            args=SimpleNamespace(report=report,database_name='metering_upgrade_fixture',database='test-db',service_user=None)
            with patch.object(perf.subprocess,'check_output',return_value='perf-one|1|2|3|5\n') as query:
                self.assertEqual(perf.verify(args),0)
                command=query.call_args.args[0]
                self.assertEqual(command[command.index('-d')+1],'metering_upgrade_fixture')
            args.database_name='host=another-server'
            with self.assertRaises(ValueError):perf.verify(args)

    def test_bounded_aggregate_report_reconciles_without_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'report.json'
            report.write_text(json.dumps({'schema':'enmaas-mock-counts-v1',
                                          'completed_mock_requests':{'perf-one':2,'perf-two':0}}))
            args=SimpleNamespace(report=report,database_name='metering',database='test-db',service_user=None)
            with patch.object(perf.subprocess,'check_output',return_value='perf-one|2|4|6|10\n'):
                self.assertEqual(perf.verify(args),0)
            for counts in ({"bad'identity":1}, {'perf-one':-1}, {'perf-one':True}, {}):
                report.write_text(json.dumps({'schema':'enmaas-mock-counts-v1','completed_mock_requests':counts}))
                with patch.object(perf.subprocess,'check_output') as query:
                    with self.assertRaises(ValueError): perf.verify(args)
                    query.assert_not_called()

    def test_multiple_streams_per_user_and_expected_throttling(self):
        class Reply:
            status=429
            def read(self,*args): return b'limited'
        class Connection:
            def __init__(self,*args,**kwargs): self.sock=None
            def request(self,*args,**kwargs): pass
            def getresponse(self): return Reply()
            def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(url='https://localhost:8443',ca=Path('unused'),tokens=Path(directory),output=Path(directory)/'report.json',model='fixture',json=True,keep_alive=False,duration=None,requests=2,lanes=3,think=0,timeout=5,concurrency=[2],allow_rate_limited=True)
            with patch.object(perf,'identities',return_value=[('perf-one','test'),('perf-two','test')]),patch.object(perf.ssl,'create_default_context'),patch.object(perf.http.client,'HTTPSConnection',Connection):
                self.assertEqual(perf.run(args),0)
            phase=json.loads(args.output.read_text())['phases'][0]
            self.assertEqual(phase['attempts'],12)
            self.assertEqual(phase['in_flight_limit'],6)
            self.assertEqual(phase['statuses'],{'429':12})
            self.assertEqual(phase['successes'],0)
            self.assertEqual({s['subject'] for s in phase['samples']},{'perf-one','perf-two'})


if __name__=='__main__': unittest.main()
