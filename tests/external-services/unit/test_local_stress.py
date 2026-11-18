import asyncio
import importlib.util
from pathlib import Path
import threading
import tempfile
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from stress_plan import plan

spec=importlib.util.spec_from_file_location('local_stress',(Path(__file__).resolve().parents[1] / 'local-stress.py'))
stress=importlib.util.module_from_spec(spec);spec.loader.exec_module(stress)

class LocalStress(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        c=MagicMock();c.callers=[('synthetic','key')]*2500
        c.profiles=[{'api':'openai'}]*2500;c.summary={};c.database_profile=False
        return c

    async def test_generator_limit_skips_escalation_but_runs_recovery(self):
        c=self.controller()
        limited={'offered_slots':100,'skipped_concurrency':50,'statuses':{'200':50},'errors':{}}
        healthy={'offered_slots':10,'statuses':{'200':10},'errors':{}}
        transport=MagicMock();transport.__aenter__=AsyncMock(return_value=AsyncMock());transport.__aexit__=AsyncMock()
        with patch.object(stress.base.LocalController,'provision_users'),patch.object(stress,'AsyncTransport',return_value=transport),patch.object(stress,'run_phase',AsyncMock(side_effect=[limited,healthy])),patch.object(stress.asyncio,'sleep',AsyncMock()):
            result=await stress.exercise(c,plan(duration=1),threading.Event())
        self.assertEqual(result,'generator_concurrency_limit')
        self.assertEqual([call.args[0]['name'] for call in c.phase.call_args_list],['stress_10','stress_recovery'])
        self.assertEqual(c.persist.call_count,2)

    async def test_database_profile_records_phase_only_after_valid_comparison(self):
        c=self.controller();c.database_profile=True
        profiler=MagicMock();profiler.snapshot.side_effect=[{'stats_reset':'epoch','rows':[]},{'stats_reset':'epoch','rows':[]}]
        profiler.compare.return_value=[]
        transport=MagicMock();transport.__aenter__=AsyncMock(return_value=AsyncMock());transport.__aexit__=AsyncMock()
        with patch.object(stress.base.LocalController,'provision_users'),patch.object(stress.base,'load',return_value=profiler),patch.object(stress.lab,'write') as write,patch.object(stress,'AsyncTransport',return_value=transport),patch.object(stress,'run_phase',AsyncMock(return_value={'offered_slots':10,'statuses':{'200':10},'errors':{}})),patch.object(stress.asyncio,'sleep',AsyncMock()):
            await stress.exercise(c,[plan(duration=1)[0]],threading.Event())
        self.assertEqual(profiler.snapshot.call_count,2)
        profiler.compare.assert_called_once()
        self.assertEqual(write.call_args.args[1],'stress_10-database.json')
        self.assertEqual(write.call_args.args[2]['statements'],[])
        self.assertIn('not phase maximum',write.call_args.args[2]['maximum_scope'])

    async def test_phase_response_duration_is_forwarded_to_transport(self):
        c=self.controller()
        transport=MagicMock();transport.__aenter__=AsyncMock(return_value=AsyncMock());transport.__aexit__=AsyncMock()
        row=dict(plan(duration=1)[0],minimum_response_seconds=10,maximum_response_seconds=10)
        with patch.object(stress.base.LocalController,'provision_users'),patch.object(stress,'AsyncTransport',return_value=transport) as factory,patch.object(stress,'run_phase',AsyncMock(return_value={'offered_slots':10,'statuses':{'200':10},'errors':{}})),patch.object(stress.asyncio,'sleep',AsyncMock()):
            await stress.exercise(c,[row],threading.Event())
        self.assertEqual(factory.call_args.kwargs['minimum_seconds'],10)
        self.assertEqual(factory.call_args.kwargs['maximum_seconds'],10)

    async def test_sampler_failure_cancels_inflight_work(self):
        c=self.controller();c.observe.side_effect=RuntimeError('safety stop')
        cancelled=asyncio.Event()
        async def request(*args,**kwargs):
            kwargs['sample']({'active_requests':7000,'dispatched':7100})
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        transport=MagicMock();transport.__aenter__=AsyncMock(return_value=AsyncMock());transport.__aexit__=AsyncMock()
        with patch.object(stress.base.LocalController,'provision_users'),patch.object(stress,'AsyncTransport',return_value=transport),patch.object(stress,'run_phase',request),patch.object(stress.asyncio,'sleep',AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,'safety stop'):
                await stress.exercise(c,plan(duration=1),threading.Event())
        self.assertTrue(cancelled.is_set())
        c.persist.assert_not_called()
        self.assertEqual(c.record_generator.call_args.args[1]['active_requests'],7000)

class MemoryExperiment(unittest.TestCase):
    def test_warm_stages_reuse_only_identical_config_and_limits(self):
        c=stress.Controller('fixture');c.keep_gateway_running=True;c.gateway_memory_mib=8192
        with patch.object(stress.base.LocalController,'restart') as parent,patch.object(stress.fixture,'run'):
            c.restart(b'first');c.restart(b'first');self.assertEqual(parent.call_count,1)
            c.restart(b'changed');self.assertEqual(parent.call_count,2)
            c.gateway_memory_mib=4096;c.restart(b'changed');self.assertEqual(parent.call_count,3)
            c.restoring=True;c.restart(b'changed');self.assertEqual(parent.call_count,4)

    def test_override_is_applied_only_during_experiment(self):
        for limit in (512,1024,2048,4096,8192,16384,32768):
            with self.subTest(memory_mib=limit):
                c=stress.Controller('fixture');c.gateway_memory_mib=limit;c.original_gateway_memory=536870912
                with patch.object(stress.base.LocalController,'restart') as parent,patch.object(stress.fixture,'run') as command:
                    c.restart(b'{}')
                    command.assert_called_once_with('update','--memory',str(limit)+'m',stress.lab.POD+'-gateway-sso')
                    command.reset_mock();c.restoring=True;c.restart(b'{}')
                    command.assert_called_once_with('update','--memory','536870912',stress.lab.POD+'-gateway-sso')
                    self.assertEqual(parent.call_count,2)

    def test_mock_memory_override_keeps_original_restore_command(self):
        with tempfile.TemporaryDirectory() as directory:
            c=stress.Controller('fixture');c.output=Path(directory);c.provider_memory_mib=2048
            original=['podman','run','--memory','512m','original-mock']
            record={'Config':{'Labels':{'io.enmaas.test-lab':stress.lab.INSTANCE},'CreateCommand':original}}
            with patch.object(stress.fixture,'run',return_value=json.dumps([record]).encode()) as command:
                c.replace_provider('new-mock')
                call=command.call_args.args
                self.assertEqual(call[call.index('--memory')+1],'2048m')
                self.assertIn('--read-only',call);self.assertIn('no-new-privileges',call)
            self.assertEqual(c.provider,original)
            with patch.object(stress.fixture,'run'),patch.object(stress.subprocess,'run',return_value=MagicMock(returncode=0)) as run,patch.object(stress.base.LocalController,'cleanup',return_value=True):
                self.assertTrue(c.cleanup())
                self.assertEqual(run.call_args_list[0].args[0],original)
            c.provider_memory_mib=0
            with patch.object(stress.fixture,'run') as command:
                with self.assertRaises(ValueError):c.replace_provider('new-mock')
                command.assert_not_called()

class FailureEvidence(unittest.TestCase):
    def test_snapshot_survives_without_a_completed_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            c=stress.Controller('fixture');c.output=Path(directory)
            c.record_generator({'name':'stress_1000'},{'active_requests':7200,'dispatched':8100})
            latest=json.loads((c.output/'generator-latest.json').read_text())
            history=json.loads((c.output/'generator.jsonl').read_text())
            self.assertEqual(latest,history)
            self.assertEqual(latest['phase'],'stress_1000')
            self.assertEqual(latest['active_requests'],7200)
            self.assertGreater(latest['sample_unix_seconds'],0)

class LedgerReconciliation(unittest.TestCase):
    def test_batched_comparison_checks_each_identity_and_all_usage_fields(self):
        c=stress.Controller('fixture');subject=c.name+'-0@example.test'
        c.summary['completed_mock_requests']={subject:2};c.tokens={subject:(2,3)}
        c.costs={subject:stress.Decimal('0.000008')}
        valid=(subject+'|2|4|6|2|0.000016\n').encode()
        with patch.object(stress.fixture,'run',return_value=valid) as run,patch.object(c,'save'):
            self.assertTrue(c.verify());self.assertEqual(run.call_count,1)
        for raw in (b'',valid.replace(b'|4|',b'|3|'),valid.replace(b'|2|0.000016',b'|1|0.000016'),valid.replace(b'0.000016',b'0.000015'),valid+(c.name+'-1@example.test|1|2|3|1|0.000008\n').encode()):
            with patch.object(stress.fixture,'run',return_value=raw),patch.object(c,'save'),patch.object(stress.time,'sleep'):
                self.assertFalse(c.verify())
        c.summary['completed_mock_requests']={'foreign':1}
        with patch.object(stress.fixture,'run') as run:
            with self.assertRaises(ValueError):c.verify()
            run.assert_not_called()
