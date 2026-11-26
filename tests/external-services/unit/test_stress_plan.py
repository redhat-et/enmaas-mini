"""The capacity staircase must distinguish offered load from achieved throughput."""
import unittest
from stress_plan import plan, escalation_stop, select_phases

class StressPlan(unittest.TestCase):
    def test_default_staircase_and_recovery(self):
        rows=plan()
        self.assertEqual([p['offered_rps'] for p in rows],[10,100,1000,10000,10])
        self.assertEqual([p['duration'] for p in rows],[600]*5)
        self.assertEqual([p['users'] for p in rows],[25,250,2500,2500,25])
        self.assertTrue(all(p['rate_limit_rps']>p['offered_rps'] for p in rows))
        self.assertEqual(rows[-1]['name'],'stress_recovery')
        self.assertTrue(all(p['concurrency']<=5000 for p in rows))

    def test_gradual_staircase_keeps_population_and_policy_fixed(self):
        rows=plan(profile='gradual',concurrency=10000)
        self.assertEqual([r['offered_rps'] for r in rows],[10,50,100,150,10])
        self.assertEqual({r['users'] for r in rows},{2500})
        self.assertEqual({r['rate_limit_rps'] for r in rows},{1000})
        self.assertEqual({r['concurrency'] for r in rows},{10000})
        self.assertEqual(rows[-1]['name'],'stress_recovery')
        with self.assertRaises(ValueError):plan(profile='unbounded')

    def test_full_profile_includes_low_load_and_retains_final_recovery(self):
        rows=plan(profile='full',concurrency=40000)
        self.assertEqual([r['offered_rps'] for r in rows],[10,50,100,150,200,250,10])
        self.assertEqual([r['name'] for r in rows if r['name']=='stress_recovery'],['stress_recovery'])
        self.assertEqual(rows[-1]['name'],'stress_recovery')
        self.assertEqual({r['rate_limit_rps'] for r in rows},{1000})

    def test_higher_profile_is_bounded_and_can_isolate_500_rps(self):
        rows=plan(profile='higher',concurrency=40000)
        self.assertEqual([r['offered_rps'] for r in rows],[500,750,1000,10])
        self.assertEqual({r['concurrency'] for r in rows},{40000})
        self.assertTrue(all(r['rate_limit_rps']>r['offered_rps'] for r in rows))
        selected=select_phases(rows,minimum=500,maximum=500)
        self.assertEqual([r['name'] for r in selected],['stress_500','stress_recovery'])

    def test_targeted_probe_preserves_recovery(self):
        rows=select_phases(plan(profile='extended',concurrency=40000),minimum=200,maximum=200)
        self.assertEqual([row['offered_rps'] for row in rows],[200,10])
        self.assertEqual(rows[-1]['name'],'stress_recovery')
        self.assertEqual({row['concurrency'] for row in rows},{40000})
        for minimum,maximum in ((250,100),(300,400),(True,200)):
            with self.assertRaises(ValueError):
                select_phases(plan(profile='extended'),minimum=minimum,maximum=maximum)

    def test_invalid_bounds_rejected(self):
        for kwargs in ({'duration':0},{'duration':3601},{'concurrency':40001},{'concurrency':True}):
            with self.assertRaises(ValueError):plan(**kwargs)

    def test_capacity_limit_is_not_gateway_failure(self):
        row={'offered_slots':1000,'dispatched':100,'skipped_concurrency':900,
             'skipped_scheduler':0,'statuses':{'200':100},'errors':{}}
        self.assertEqual(escalation_stop(row),'generator_concurrency_limit')
        row.update(skipped_concurrency=0,skipped_scheduler=900)
        self.assertEqual(escalation_stop(row),'generator_scheduler_limit')
        row.update(skipped_scheduler=0,dispatched=1000,statuses={'200':1000})
        self.assertIsNone(escalation_stop(row))
        row.update(statuses={'200':990,'503':10})
        self.assertEqual(escalation_stop(row),'service_errors')
        row.update(statuses={'200':990,'429':10})
        self.assertEqual(escalation_stop(row),'unexpected_policy_denials')
        row.update(statuses={'200':990,'0':10},errors={'TimeoutError':10})
        self.assertEqual(escalation_stop(row),'request_errors')

class ExpandedCapacity(unittest.TestCase):
    def test_expanded_profile_reaches_above_old_generator_ceiling(self):
        rows=plan(profile='extended',concurrency=40000)
        self.assertEqual([r['offered_rps'] for r in rows],[100,150,200,250,10])
        self.assertEqual({r['concurrency'] for r in rows},{40000})
        self.assertEqual(rows[-1]['name'],'stress_recovery')
        with self.assertRaises(ValueError):plan(concurrency=40001)

if __name__=='__main__':unittest.main()
