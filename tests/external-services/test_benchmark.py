#!/usr/bin/env python3
"""Orchestration failure tests without root, credentials or a live deployment."""
import importlib.util
from pathlib import Path
import threading
import sys
import time
import tempfile
import json
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from contextlib import contextmanager

spec=importlib.util.spec_from_file_location('benchmark_lab',Path(__file__).with_name('benchmark.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Controller:
    def __init__(self, failure=None): self.events=[];self.failure=failure;self.results=[]
    def prepare(self,count):
        self.events.append('prepare')
        if self.failure=='prepare': raise RuntimeError('partial provisioning')
    def phase(self,phase): self.events.append('phase')
    def observe(self):
        self.events.append('observe')
        if self.failure=='observe': raise RuntimeError('guardrail')
    def guard(self): pass
    def sample_due(self): return False
    @contextmanager
    def transport(self): yield lambda user,timeout: {'status':200}
    def persist(self,phase,result): self.events.append('persist');self.results.append(result)
    def verify(self): self.events.append('verify');return self.failure!='verify'
    def cleanup(self): self.events.append('cleanup');return self.failure!='cleanup'

class Orchestration(unittest.TestCase):
    def plan(self): return {'mock_only':True,'phases':[{'name':'baseline','users':1,'streams_per_user':1,'rate_limit_rps':100}]}
    def test_success_always_reconciles_and_cleans(self):
        c=Controller();m.run_plan(c,self.plan(),duration=.03,offered_rps=100,timeout=.1)
        self.assertEqual(c.events[-2:],['verify','cleanup'])
        self.assertEqual(len(c.results),1)
    def test_partial_provisioning_still_cleans(self):
        c=Controller('prepare')
        with self.assertRaises(RuntimeError):m.run_plan(c,self.plan(),duration=.03,offered_rps=100,timeout=.1)
        self.assertEqual(c.events,['prepare','cleanup'])
    def test_guardrail_and_reconciliation_failures_still_clean(self):
        for failure in ('observe','verify','cleanup'):
            c=Controller(failure)
            with self.assertRaises(RuntimeError):m.run_plan(c,self.plan(),duration=.03,offered_rps=100,timeout=.1)
            self.assertEqual(c.events[-1],'cleanup')
    def test_cancelled_run_never_starts_another_phase(self):
        stop=threading.Event();stop.set();c=Controller()
        with self.assertRaises(InterruptedError):m.run_plan(c,self.plan(),duration=.03,offered_rps=100,timeout=.1,stop=stop)
        self.assertNotIn('phase',c.events);self.assertEqual(c.events[-1],'cleanup')
    def test_bad_plan_cannot_touch_deployment(self):
        for change in ({'mock_only':False},{'phases':[]},{'phases':[{'name':'../../bad'}]}):
            c=Controller();plan=self.plan();plan.update(change)
            with self.assertRaises(ValueError):m.run_plan(c,plan,duration=.03,offered_rps=100,timeout=.1)
            self.assertEqual(c.events,[])

class ManagedProcess(unittest.TestCase):
    def test_cancellation_stops_the_child(self):
        stop=threading.Event()
        timer=threading.Timer(.1,stop.set);timer.start()
        started=time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                m.managed_command([sys.executable,'-c','import time; time.sleep(10)'],timeout=5,stop=stop)
        finally: timer.cancel()
        self.assertLess(time.monotonic()-started,2)

    def test_deadline_and_nonzero_exit(self):
        with self.assertRaises(TimeoutError):
            m.managed_command([sys.executable,'-c','import time; time.sleep(10)'],timeout=.1)
        self.assertEqual(m.managed_command([sys.executable,'-c','raise SystemExit(7)'],timeout=2),7)

class RhelCleanup(unittest.TestCase):
    def test_partial_user_is_retired_even_if_config_restore_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            c=m.RhelController.__new__(m.RhelController)
            c.output=Path(directory);c.original=b'original';c.provisioned=True;c.name='perf-owned';c.lock=None
            c.replace_config=Mock(side_effect=RuntimeError('SECRET_NOT_FOR_REPORT'))
            c.restart=Mock()
            calls=[]
            def request(method,resource,payload=None):
                calls.append((method,resource,payload))
                if method=='GET':return [{'user_id':'perf-owned-00'},{'user_id':'unrelated'}]
            c.admin=SimpleNamespace(request=request)
            self.assertFalse(c.cleanup())
            self.assertIn(('PUT','users',{'subject':'perf-owned-00','active':False}),calls)
            self.assertIn(('POST','key-revocations',{'subject':'perf-owned-00'}),calls)
            self.assertEqual(len(calls),3)
            result=(c.output/'cleanup.json').read_text()
            self.assertNotIn('SECRET',result)
            self.assertFalse(json.loads(result)['complete'])

    def test_real_deployment_rejected_before_provisioning(self):
        c=m.RhelController.__new__(m.RhelController)
        c.bench=SimpleNamespace(require_mock=Mock(side_effect=ValueError('real provider')))
        with self.assertRaises(ValueError):c.prepare(20)
        c.bench.require_mock.assert_called_once_with(m.DEPLOY)

if __name__=='__main__':unittest.main()
