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
from unittest.mock import Mock, patch
import unittest
from contextlib import contextmanager

spec=importlib.util.spec_from_file_location('benchmark_lab',(Path(__file__).resolve().parents[1] / 'benchmark.py'))
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
    def test_detached_run_uses_fixed_systemd_command_without_secret_environment(self):
        args=SimpleNamespace(duration=600,offered_rps=100,timeout=30,database_name="metering_clone")
        guard=Mock()
        with patch.object(m,'load',return_value=guard),patch.object(m.subprocess,'run',return_value=Mock(returncode=0)) as run:
            unit=m.launch_detached(args,self.plan())
        guard.require_mock.assert_called_once_with(m.DEPLOY)
        argv=run.call_args.args[0]
        self.assertRegex(unit,r'^enmaas-mock-benchmark-[a-f0-9]{12}\.service$')
        self.assertIn('--unit='+unit,argv)
        self.assertIn('--property=KillMode=mixed',argv)
        self.assertIn('--property=TimeoutStopSec=180',argv)
        self.assertIn('--property=UMask=0077',argv)
        self.assertNotIn('--detach',argv)
        self.assertEqual(argv[argv.index('--database-name')+1],'metering_clone')
        self.assertEqual(run.call_args.kwargs['timeout'],30)
        self.assertEqual(run.call_args.kwargs['stderr'],m.subprocess.DEVNULL)
        self.assertNotIn('--setenv',str(argv))

    def test_detached_failure_is_not_retried_or_started_for_real_models(self):
        args=SimpleNamespace(duration=600,offered_rps=100,timeout=30,database_name="metering_clone")
        guard=Mock();guard.require_mock.side_effect=ValueError('real provider')
        with patch.object(m,'load',return_value=guard),patch.object(m.subprocess,'run') as run,self.assertRaises(ValueError):
            m.launch_detached(args,self.plan())
        run.assert_not_called()
        for failure in (Mock(returncode=1),OSError('private output'),m.subprocess.TimeoutExpired('systemd',30)):
            with patch.object(m,'load',return_value=Mock()),patch.object(m.subprocess,'run',side_effect=failure if isinstance(failure,Exception) else [failure]) as run,self.assertRaisesRegex(RuntimeError,'inspect the printed unit'):
                m.launch_detached(args,self.plan())
            self.assertEqual(run.call_count,1)
        args.duration=0
        with patch.object(m,'load') as guard,self.assertRaises(ValueError):m.launch_detached(args,self.plan())
        guard.assert_not_called()

    def test_success_always_reconciles_and_cleans(self):
        c=Controller();m.run_plan(c,self.plan(),duration=.03,offered_rps=100,timeout=.1)
        self.assertEqual(c.events[-2:],['verify','cleanup'])
        self.assertEqual(len(c.results),1)
    def test_cycles_and_phase_rates_are_bounded_and_reported(self):
        c=Controller();plan=self.plan();plan['phases'][0]['offered_rps']=50
        result={'interrupted':False,'errors':{},'statuses':{'200':1}}
        with patch.object(m.scheduler,'run_phase',return_value=result) as execute:
            m.run_plan(c,plan,duration=.03,offered_rps=100,timeout=.1,cycles=2)
        self.assertEqual(execute.call_count,2)
        self.assertEqual(execute.call_args.kwargs['offered_rps'],50)
        self.assertEqual(len(c.results),2)
        for cycles in (0,13,True):
            c=Controller()
            with self.assertRaises(ValueError):m.run_plan(c,plan,cycles=cycles)
            self.assertEqual(c.events,[])
        for rate in (0,2001,float('nan'),True):
            plan['phases'][0]['offered_rps']=rate
            with self.assertRaises(ValueError):m.validate_plan(plan,60,100,30)

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

    def test_cleanup_uses_logins_and_attempts_recorded_users_when_listing_fails(self):
        for fail in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                c=m.RhelController.__new__(m.RhelController)
                c.output=Path(directory);c.original=None;c.provisioned=True;c.name='perf-owned';c.lock=None
                c.created=['perf-owned-1@example.test']
                calls=[]
                def request(method,resource,payload=None):
                    calls.append((method,resource,payload))
                    if method=='GET':
                        if fail: raise RuntimeError('unavailable')
                        return [{'user_id':'some-uuid','logins':['perf-owned-2@example.test','someone-else']}]
                c.admin=SimpleNamespace(request=request)
                self.assertEqual(c.cleanup(),not fail)
                revoked=[p['subject'] for method,resource,p in calls if resource=='key-revocations']
                self.assertIn('perf-owned-1@example.test',revoked)
                self.assertNotIn('some-uuid',revoked)
                self.assertNotIn('someone-else',revoked)
                if not fail:self.assertIn('perf-owned-2@example.test',revoked)

    def test_price_preflight_records_safe_failure_without_service_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            c=m.RhelController.__new__(m.RhelController)
            c.output=Path(directory);c.summary={};c.database_name='metering_clone';c.original=None;c.provisioned=False;c.lock=None
            c.perf=SimpleNamespace(mock_request_cost=Mock(side_effect=ValueError('SECRET_RESPONSE')))
            c.replace_config=Mock();c.restart=Mock();c.admin=Mock()
            with self.assertRaisesRegex(RuntimeError,'mock price preflight failed'):
                c.price_preflight('fixture')
            report=(c.output/'summary.json').read_text()
            self.assertNotIn('SECRET',report)
            self.assertEqual(json.loads(report)['failure_stage'],'mock_price_preflight')
            self.assertTrue(c.cleanup())
            c.replace_config.assert_not_called();c.restart.assert_not_called()
            c.admin.request.assert_not_called()

    def test_price_preflight_returns_verified_cost(self):
        c=m.RhelController.__new__(m.RhelController)
        c.database_name='metering_clone'
        c.perf=SimpleNamespace(mock_request_cost=Mock(return_value='0.00000005'))
        self.assertEqual(c.price_preflight('fixture'),'0.00000005')
        self.assertEqual(c.perf.mock_request_cost.call_args.args[1],'fixture')
        self.assertEqual(c.perf.mock_request_cost.call_args.args[0].database_name,'metering_clone')

    def test_database_name_validation_before_loading_host_dependencies(self):
        for value in ('', 'postgres;DROP', 'a'*64, 'bad-name', '/tmp/db'):
            with patch.object(m,'load') as load,self.assertRaises(ValueError):
                m.RhelController(database_name=value)
            load.assert_not_called()

    def test_sampler_uses_selected_database(self):
        with tempfile.TemporaryDirectory() as directory:
            c=m.RhelController.__new__(m.RhelController)
            c.output=Path(directory);c.database_name='metering_clone';c.prefix=['service-prefix']
            c.guard=Mock();c.observer=SimpleNamespace(sample=Mock(return_value={'utc':'test'}))
            c.bench=SimpleNamespace(check_sample=Mock(return_value={'gateway':0}));c.restarts=None
            c.observe()
            c.observer.sample.assert_called_once_with(['service-prefix'],'metering_clone')
            self.assertEqual(c.restarts,{'gateway':0})

    def test_reconciliation_uses_selected_database(self):
        with tempfile.TemporaryDirectory() as directory:
            c=m.RhelController.__new__(m.RhelController)
            c.output=Path(directory);c.database_name='metering_clone';c.summary={};c.stop=None
            with patch.object(m,'managed_command',return_value=0) as call:
                self.assertTrue(c.verify())
            argv=call.call_args.args[0]
            self.assertEqual(argv[argv.index('--database-name')+1],'metering_clone')

    def test_real_deployment_rejected_before_provisioning(self):
        c=m.RhelController.__new__(m.RhelController)
        c.bench=SimpleNamespace(require_mock=Mock(side_effect=ValueError('real provider')))
        with self.assertRaises(ValueError):c.prepare(20)
        c.bench.require_mock.assert_called_once_with(m.DEPLOY)

if __name__=='__main__':unittest.main()


class BudgetMixEvidence(unittest.TestCase):
    def test_both_account_types_must_actually_hit_budget(self):
        summary={'completed_mock_requests':{'a':10,'b':20},'synthetic_personas':{
            'a':{'account_type':'person'},'b':{'account_type':'bot'}},
            'phases':[{'per_user_budget_denials':[2,0],'per_user_rate_limit_denials':[0,50]}]}
        with self.assertRaises(RuntimeError):m.verify_budget_mix(summary)
        summary['phases'][0]['per_user_budget_denials'][1]=1
        m.verify_budget_mix(summary)
        self.assertTrue(summary['budget_mix_verified'])
