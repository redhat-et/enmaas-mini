#!/usr/bin/env python3
"""Deadline, backpressure, interruption and bounded result contracts."""
import importlib.util
from pathlib import Path
import threading
import time
import unittest

spec = importlib.util.spec_from_file_location('scenarios', Path(__file__).with_name('scenarios.py'))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

class Scenarios(unittest.TestCase):
    def test_fast_denials_do_not_finish_phase_early(self):
        report = s.run_phase(lambda user, timeout: {'status':429}, users=2, lanes=2,
                             duration=.15, offered_rps=100, timeout=.1)
        self.assertGreaterEqual(report['admission_seconds'], .14)
        self.assertGreater(report['attempts'], 3)
        self.assertLessEqual(report['attempts'], 17)
        self.assertEqual(report['statuses'], {'429':report['attempts']})

    def test_burst_windows(self):
        self.assertTrue(s.active_at(0, .1, .2))
        self.assertTrue(s.active_at(.099, .1, .2))
        self.assertFalse(s.active_at(.1, .1, .2))
        self.assertFalse(s.active_at(.299, .1, .2))
        self.assertTrue(s.active_at(.301, .1, .2))

    def test_concurrency_is_per_user_bounded(self):
        lock = threading.Lock()
        current = {}; peaks = {}
        def request(user, timeout):
            with lock:
                current[user] = current.get(user, 0)+1
                peaks[user] = max(peaks.get(user, 0), current[user])
            time.sleep(.01)
            with lock: current[user] -= 1
            return {'status':200}
        result = s.run_phase(request, users=3, lanes=2, duration=.12, offered_rps=500, timeout=.1)
        self.assertEqual(set(peaks), {0,1,2})
        self.assertTrue(all(v <= 2 for v in peaks.values()))
        self.assertGreater(result['attempts'], 0)
        self.assertEqual(sum(result['per_user_successes']), result['attempts'])

    def test_stop_and_errors_are_recorded_without_exception_text(self):
        stop = threading.Event()
        def request(user, timeout):
            stop.set()
            raise ValueError('SECRET_MUST_NOT_APPEAR')
        result = s.run_phase(request, users=1, lanes=1, duration=10, offered_rps=100,
                             timeout=.1, stop=stop)
        self.assertTrue(result['interrupted'])
        self.assertEqual(result['errors'], {'ValueError':1})
        self.assertNotIn('SECRET', str(result))
        self.assertLess(result['elapsed_seconds'], 1)

    def test_invalid_or_unbounded_inputs_are_rejected(self):
        for change in ({'duration':0}, {'users':101}, {'lanes':33}, {'offered_rps':float('inf')},
                       {'timeout':0}, {'active':1}, {'idle':1}):
            args = dict(users=1, lanes=1, duration=1, offered_rps=10, timeout=1)
            args.update(change)
            with self.assertRaises(ValueError): s.run_phase(lambda *a: {}, **args)

if __name__ == '__main__': unittest.main()
