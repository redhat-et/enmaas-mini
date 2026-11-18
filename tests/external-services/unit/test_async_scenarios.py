import asyncio
import unittest

import async_scenarios


class AsyncScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_concurrency_is_bounded_and_saturation_is_counted(self):
        active = peak = 0
        async def request(user, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(.03)
                return {'status': 200, 'bytes': 4, 'first_byte_seconds': .01, 'request_bytes':8, 'stream_events':2}
            finally:
                active -= 1
        result = await async_scenarios.run_phase(request, users=3, concurrency=2,
            duration=.15, offered_rps=500, timeout=1)
        self.assertLessEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertEqual(result['request_bytes'],result['attempts']*8)
        self.assertEqual(result['stream_events'],result['attempts']*2)
        self.assertGreater(result['skipped_concurrency'], 0)
        self.assertEqual(result['dispatched'], sum(result['per_user_attempts']))
        self.assertEqual(result['offered_slots'], result['dispatched'] +
            result['skipped_concurrency'] + result['skipped_scheduler'])

    async def test_deadlines_are_enforced_without_retaining_exception_messages(self):
        async def request(user, timeout):
            await asyncio.sleep(10)
        result = await async_scenarios.run_phase(request, users=1, concurrency=2,
            duration=.05, offered_rps=100, timeout=.02)
        self.assertGreater(result['errors'].get('TimeoutError', 0), 0)
        self.assertEqual(result['statuses'].get('200', 0), 0)

    async def test_large_cap_does_not_force_large_offered_load(self):
        async def request(user, timeout):return {'status':200}
        result=await async_scenarios.run_phase(request,users=1,concurrency=40000,
            duration=.02,offered_rps=10,timeout=1)
        self.assertLessEqual(result['peak_in_flight'],1)
        self.assertEqual(result['skipped_concurrency'],0)

    async def test_completed_tasks_waiting_for_callbacks_are_not_active(self):
        from unittest.mock import patch
        class DeferredDiscardTask(asyncio.Task):
            def add_done_callback(self, callback, *, context=None):
                if getattr(callback, '__name__', '') == 'discard':
                    def delayed(task):
                        self.get_loop().call_later(.2, callback, task)
                    return super().add_done_callback(delayed, context=context)
                return super().add_done_callback(callback, context=context)
        samples=[]
        async def request(user, timeout):return {'status':200}
        with patch.object(async_scenarios.asyncio, 'create_task', side_effect=DeferredDiscardTask):
            result=await async_scenarios.run_phase(request, users=1, concurrency=1,
                duration=1.15, offered_rps=10, timeout=1, sample=samples.append)
        self.assertEqual(result['skipped_concurrency'], 0)
        self.assertEqual(result['peak_in_flight'], 1)
        self.assertEqual(result['statuses'], {'200':11})
        for snapshot in samples:
            self.assertEqual(snapshot['dispatched'], snapshot['completed']+snapshot['active_requests'])

    async def test_bounds_and_external_cancellation(self):
        async def request(user, timeout):
            await asyncio.sleep(10)
        with self.assertRaises(ValueError):
            await async_scenarios.run_phase(request, users=1, concurrency=40001,
                duration=1, offered_rps=10000, timeout=150)
        task = asyncio.create_task(async_scenarios.run_phase(request, users=1,
            concurrency=2, duration=10, offered_rps=100, timeout=150))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_samples_continue_during_drain_and_end_with_zero_active(self):
        samples=[]
        async def request(user,timeout):
            await asyncio.sleep(1.2)
            return {'status':200}
        result=await async_scenarios.run_phase(request,users=1,concurrency=1,
            duration=.1,offered_rps=10,timeout=2,sample=samples.append)
        self.assertTrue(any(s['elapsed_seconds']>1 and s['active_requests']==1 for s in samples))
        self.assertEqual(samples[-1]['active_requests'],0)
        self.assertEqual(samples[-1]['completed'],result['attempts'])

    async def test_snapshots_preserve_outcome_counts_without_exception_text(self):
        samples=[]
        async def request(user,timeout):
            if user==0:raise ValueError('credential-sentinel-must-not-be-retained')
            return {'status':503}
        result=await async_scenarios.run_phase(request,users=2,concurrency=4,
            duration=.1,offered_rps=50,timeout=1,sample=samples.append)
        self.assertEqual(samples[-1]['statuses'],result['statuses'])
        self.assertEqual(samples[-1]['errors'],result['errors'])
        self.assertEqual(samples[0]['statuses'],{})
        self.assertNotIn('credential-sentinel',str(samples))

    async def test_os_error_categories_never_retain_exception_details(self):
        import errno
        samples=[]
        class ConnectorFailure(Exception):
            os_error=OSError(errno.EADDRNOTAVAIL,'private URL or credential')
        async def request(user,timeout):raise ConnectorFailure('private URL or credential')
        result=await async_scenarios.run_phase(request,users=1,concurrency=2,
            duration=.05,offered_rps=100,timeout=1,sample=samples.append)
        self.assertGreater(result['os_errors']['EADDRNOTAVAIL'],0)
        self.assertEqual(samples[-1]['os_errors'],result['os_errors'])
        self.assertNotIn('private',str(result)+str(samples))
