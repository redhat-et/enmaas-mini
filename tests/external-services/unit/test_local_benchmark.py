#!/usr/bin/env python3
"""Local benchmark cleanup and independent USD verification contracts."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('local_benchmark',(Path(__file__).resolve().parents[1] / 'local-benchmark.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class LocalBenchmark(unittest.TestCase):
    def test_failed_preflight_never_provisions_users_or_changes_policies(self):
        with patch('sys.argv', ['local-benchmark.py', '--praxis-image', 'fixture-image']), \
                patch.object(module.os, 'umask'), patch.object(module.signal, 'signal'), \
                patch.object(module.LocalController, 'guard', side_effect=module.SafetyStop('capacity')), \
                patch.object(module.bench, 'run_plan') as run:
            with self.assertRaises(module.SafetyStop):module.main()
            run.assert_not_called()

    def test_cleanup_attempts_all_retirements_and_restoration(self):
        controller=module.LocalController('fixture-image')
        controller.created=['lab-run-test-0','lab-run-test-1'];controller.original=b'{}'
        calls=[]
        def api(route,body,method):
            calls.append((route,body['subject']))
            if route=='users':raise RuntimeError('failure')
        with tempfile.TemporaryDirectory() as directory:
            controller.output=Path(directory)
            with patch.object(controller,'api',side_effect=api),patch.object(controller,'restart') as restart:
                self.assertFalse(controller.cleanup())
                restart.assert_called_once_with(b'{}')
        self.assertEqual(len(calls),4)
        self.assertEqual(sum(route=='key-revocations' for route,_ in calls),2)

    def test_restart_preserves_enabled_telemetry(self):
        controller=module.LocalController('fixture-image')
        controller.telemetry=True;controller.service_name='enmaas-test'
        with patch.object(module.fixture,'run'),patch.object(module.lab,'write'),patch.object(module.fixture,'secret'),patch.object(module.fixture,'start_gateway') as start,patch.object(module.lab,'wait_url'):
            controller.restart(b'{}')
            start.assert_called_once_with('fixture-image',telemetry=True,service_name='enmaas-test')

    def test_phase_telemetry_must_be_newer_than_restart(self):
        controller=module.LocalController('fixture-image');controller.telemetry=True
        controller.phase_started='2026-01-01T00:00:00+00:00'
        controller.callers=[('user','not-a-real-key')]
        controller.summary['completed_mock_requests']={'user':0}
        start=module.datetime.datetime.fromisoformat(controller.phase_started).timestamp()
        def response(url):
            if '9090' in url:return {'data':{'result':[{'value':[start,str(start+1)]}]}}
            return {'traces':[{'startTimeUnixNano':str(int((start+1)*1e9))}]}
        with patch.object(module.signals,'query',side_effect=response),patch.object(controller,'save'),patch.object(module.signals,'send'):
            controller.persist({'name':'baseline','rate_limit_rps':1000},{'per_user_successes':[1]})
        self.assertTrue(controller.summary['phases'][-1]['telemetry_verified'])
        with patch.object(module.signals,'query',return_value={}),patch.object(controller,'save'),patch.object(module.signals,'send'),patch.object(module.time,'sleep'):
            with self.assertRaises(RuntimeError):controller.persist({'name':'team','rate_limit_rps':1000},{'per_user_successes':[1]})
        stale={'data':{'result':[{'value':[start,str(start-1)]}]},
               'traces':[{'startTimeUnixNano':str(int((start-1)*1e9))}]}
        with patch.object(module.signals,'query',return_value=stale),patch.object(controller,'save'),patch.object(module.signals,'send'),patch.object(module.time,'sleep'):
            with self.assertRaises(RuntimeError):controller.persist({'name':'parallel','rate_limit_rps':1000},{'per_user_successes':[1]})


    def test_disk_reserve_cannot_be_disabled(self):
        for percent,gib in ((9,8),(51,8),(10,3),(True,8),(10,1001)):
            with self.assertRaises(ValueError):module.LocalController('fixture-image',min_disk_percent=percent,min_disk_gib=gib)
        controller=module.LocalController('fixture-image',min_disk_percent=10,min_disk_gib=8)
        self.assertEqual(controller.min_disk_percent,10)

    def test_successful_guard_is_sampled_at_five_second_intervals(self):
        from types import SimpleNamespace
        import json
        controller=module.LocalController('fixture-image')
        replies=[SimpleNamespace(stdout=b'MemTotal: 1000 kB\nMemAvailable: 900 kB\n'),
                 SimpleNamespace(stdout=b'Filesystem blocks Used Available Capacity Mounted on\n/dev/test 100000000 50000000 50000000 50% /var\n'),
                 SimpleNamespace(stdout=json.dumps([{'Name':module.lab.POD+'-'+name,'State':{'Running':True}} for name in ('gateway-sso','maas','metering-sso','provider','db')]).encode())]
        with patch.object(module.Path,'exists',return_value=False),patch.object(module.local_probe,'read',side_effect=replies*2) as read,patch.object(module.time,'monotonic',side_effect=[100,100,101,106,106]):
            controller.guard();self.assertEqual(read.call_count,3)
            controller.guard();self.assertEqual(read.call_count,3)
            controller.guard();self.assertEqual(read.call_count,6)

    def test_guard_checks_observability_services_when_telemetry_is_enabled(self):
        from types import SimpleNamespace
        import json
        controller=module.LocalController('fixture-image');controller.telemetry=True
        records=[{'Name':module.lab.POD+'-'+name,'State':{'Running':True}} for name in module.component_metrics.COMPONENTS]
        records[10]['State']={'Running':False,'OOMKilled':True}
        replies=[SimpleNamespace(stdout=b'MemTotal: 1000 kB\nMemAvailable: 900 kB\n'),
                 SimpleNamespace(stdout=b'Filesystem blocks Used Available Capacity Mounted on\n/dev/test 100000000 50000000 50000000 50% /var\n'),
                 SimpleNamespace(stdout=json.dumps(records).encode())]
        with patch.object(module.Path,'exists',return_value=False),patch.object(module.local_probe,'read',side_effect=replies) as read:
            with self.assertRaisesRegex(module.SafetyStop,'stopped or exceeded memory: tempo'):controller.guard()
            self.assertIn(module.lab.POD+'-tempo',read.call_args.args[0])
            self.assertIn(module.lab.POD+'-collector',read.call_args.args[0])

    def test_low_host_memory_stops_before_container_inspection(self):
        controller=module.LocalController('fixture-image')
        with patch.object(module.Path,'exists',return_value=False), patch.object(module.local_probe.subprocess,'run') as command, patch.object(module.fixture,'run') as inspect:
            command.return_value.stdout=b'MemTotal: 1000 kB\nMemAvailable: 99 kB\n'
            with self.assertRaises(RuntimeError):controller.guard()
            inspect.assert_not_called()

    def test_low_vm_disk_stops_before_container_inspection(self):
        controller=module.LocalController('fixture-image')
        from types import SimpleNamespace
        with patch.object(module.Path,'exists',return_value=False), patch.object(module.local_probe.subprocess,'run',side_effect=[
                SimpleNamespace(stdout=b'MemTotal: 1000 kB\nMemAvailable: 900 kB\n'),
                SimpleNamespace(stdout=b'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/test 1000 800 200 80% /var\n')]), patch.object(module.fixture,'run') as inspect:
            with self.assertRaisesRegex(RuntimeError, 'disk safety'):controller.guard()
            inspect.assert_not_called()

    def test_ledger_requires_exact_cost_and_every_subject(self):
        controller=module.LocalController('fixture-image')
        controller.summary['completed_mock_requests']={'lab-run-test-0':3,'lab-run-test-1':0}
        with patch.object(module.fixture,'run',side_effect=[b'3|6|9|3|0.00002400',b'0|0|0|0|0']),patch.object(controller,'save'),patch.object(module.signals,'send'):
            self.assertTrue(controller.verify())
        with patch.object(module.fixture,'run',return_value=b'3|6|9|2|0.00002400'),patch.object(module.time,'sleep'):
            self.assertFalse(controller.verify())

if __name__=='__main__':unittest.main()
