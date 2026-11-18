import json
from pathlib import Path
import unittest
from workload_profiles import persona, client_for_api, workload, budget_profile

class Profiles(unittest.TestCase):
    def test_capacity_population_is_bounded_and_unique(self):
        self.assertEqual(len({persona(i)['name'] for i in range(2500)}),2500)
        self.assertTrue(persona(2499)['name'])

    def test_deterministic_unique_names_and_bounded_safe_metadata(self):
        people = [persona(i) for i in range(100)]
        self.assertEqual(len({p['name'] for p in people}), 100)
        self.assertEqual(people, [persona(i) for i in range(100)])
        self.assertGreater(len({p['country'] for p in people}), 5)
        self.assertGreater(len({p['user_agent'] for p in people}), 3)
        for p in people:
            self.assertNotIn('\n', p['user_agent'])
            self.assertIn('synthetic', p['user_agent'])
    def test_default_traffic_covers_exactly_alices_25_reports(self):
        plan=json.loads((Path(__file__).resolve().parents[1] / 'traffic-patterns.json').read_text())
        self.assertEqual(max(p['users'] for p in plan['phases']),25)
        self.assertTrue(all(p['users'] in (1,25) for p in plan['phases']))

    def test_invalid_indices(self):
        for value in (-1, 2500, True, '1'):
            with self.assertRaises(ValueError): persona(value)

    def test_clients_match_supported_protocol(self):
        for index in range(100):
            self.assertIn('claude-code',client_for_api(index,'anthropic'))
            self.assertNotIn('claude-code',client_for_api(index,'openai'))
        with self.assertRaises(ValueError): client_for_api(0,'unknown')

    def test_service_accounts_are_explicit_but_have_no_privileges(self):
        profiles=[persona(i) for i in range(25)]
        bots=[p for p in profiles if p['account_type']=='bot']
        self.assertEqual(len(bots),10)
        self.assertEqual(sum(p['account_type']=='person' for p in profiles),15)
        for p in bots:
            self.assertIn('Bot',p['name'])
            self.assertNotIn('role',p)
            self.assertNotIn('key',p)
            self.assertEqual(p['monthly_usd'],1000)
        self.assertTrue(all(p['monthly_usd']==300 for p in profiles if p['account_type']=='person'))

    def test_workload_ranges_and_bot_job_size(self):
        rows=[workload(i) for i in range(25)]
        self.assertEqual(min(r['input_tokens'] for r in rows),512)
        self.assertEqual(max(r['input_tokens'] for r in rows),16384)
        self.assertEqual(max(r['output_tokens'] for r in rows),1536)
        self.assertGreater(len({r['ttft_ms'] for r in rows}),3)
        for i in range(15,25):self.assertIn(workload(i)['input_tokens'],(4096,8192,16384))

    def test_budget_mix_is_explicit_and_contains_people_and_bots(self):
        rows=[budget_profile(i, mixed=True) for i in range(25)]
        low=[i for i,p in enumerate(rows) if p['monthly_usd']==.01]
        self.assertTrue(any(i<15 for i in low))
        self.assertTrue(any(i>=15 for i in low))
        self.assertGreater(len({p['monthly_usd'] for p in rows}),3)
        for i in range(25):
            self.assertEqual(budget_profile(i)['monthly_usd'],persona(i)['monthly_usd'])
        self.assertGreater(len({tuple(workload(i).values()) for i in range(15,25)}),1)
