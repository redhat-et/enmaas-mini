"""Parallel workers must conserve load and use one shared elapsed interval."""
import unittest
import asyncio
from parallel_results import partition, merge


class ParallelResults(unittest.TestCase):
    def test_partition_conserves_every_user_slot_and_rate(self):
        rows=partition(users=25,concurrency=512,offered_rps=100,workers=6)
        self.assertEqual([user for row in rows for user in range(row['start'],row['end'])],list(range(25)))
        self.assertEqual(sum(row['concurrency'] for row in rows),512)
        self.assertEqual(sum(row['offered_rps'] for row in rows),100)
        self.assertLessEqual(max(row['offered_rps'] for row in rows)-min(row['offered_rps'] for row in rows),1)

    def test_invalid_or_empty_partitions_are_rejected(self):
        for change in ({'workers':0},{'workers':13},{'workers':True},{'users':3},
                       {'concurrency':3},{'offered_rps':3},{'offered_rps':1.2},
                       {'concurrency':40001},{'users':2501}):
            with self.assertRaises(ValueError):
                partition(**dict(dict(users=25,concurrency=512,offered_rps=100,workers=6),**change))

    def row(self,start,elapsed,mean,users=1):
        return dict(started_monotonic=start,elapsed_seconds=elapsed,admission_seconds=10,
            users=users,offered_rps=1,target_seconds=10,max_in_flight=3,peak_in_flight=2,
            offered_slots=10,dispatched=10,skipped_concurrency=0,skipped_scheduler=0,
            statuses={'200':10},errors={},os_errors={},attempts=10,
            per_user_attempts=[10],per_user_successes=[10],latency_bucket_upper_seconds=[1,None],
            latency_bucket_counts=[5,5],first_byte_bucket_counts=[10,0],
            response_bytes=100,request_bytes=50,stream_events=10,
            interrupted=False,mean_in_flight=mean)

    def test_merge_accounts_for_start_skew_and_weights_mean_concurrency(self):
        result=merge([self.row(100,20,2),self.row(105,30,3)])
        self.assertEqual(result['elapsed_seconds'],35)
        self.assertEqual(result['admission_seconds'],15)
        self.assertEqual(result['drain_seconds'],20)
        self.assertAlmostEqual(result['mean_in_flight'],130/35)
        self.assertEqual(result['worker_start_skew_seconds'],5)
        self.assertEqual(result['per_user_attempts'],[10,10])
        self.assertEqual(result['latency_bucket_counts'],[10,10])
        self.assertEqual(result['statuses'],{'200':20})
        self.assertAlmostEqual(result['completed_rps'],20/35)
        self.assertEqual(result['peak_in_flight_upper_bound'],4)

    def test_inconsistent_or_unbounded_worker_evidence_cannot_pass(self):
        for change in ({'statuses':{'200':9}},{'elapsed_seconds':float('nan')},
                       {'started_monotonic':-1},{'offered_slots':11},
                       {'per_user_attempts':[8]},{'latency_bucket_counts':[1,2]},
                       {'max_in_flight':True},{'errors':{'TimeoutError':1}}):
            with self.assertRaises(ValueError):merge([dict(self.row(100,20,2),**change)])


class ActualSchedulerResults(unittest.IsolatedAsyncioTestCase):
    async def test_real_scheduler_results_conserve_outcomes_and_timing(self):
        from async_scenarios import run_phase
        async def request(user,timeout):
            await asyncio.sleep(.005)
            return {'status':200,'bytes':7}
        rows=await asyncio.gather(*(run_phase(request,users=2,concurrency=4,
            duration=.1,offered_rps=50,timeout=1) for _ in range(2)))
        combined=merge(rows)
        self.assertEqual(combined['offered_slots'],10)
        self.assertEqual(combined['dispatched']+combined['skipped_scheduler']+combined['skipped_concurrency'],10)
        self.assertEqual(combined['attempts'],sum(combined['per_user_attempts']))
        self.assertGreater(combined['elapsed_seconds'],0)
        self.assertGreater(combined['mean_in_flight'],0)
        self.assertEqual(combined['response_bytes'],7*combined['attempts'])


if __name__=='__main__':unittest.main()
