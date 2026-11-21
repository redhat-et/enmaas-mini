"""Disposable transfer checks keep data off argv and clean up on every outcome."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('transfer_runtime',(Path(__file__).resolve().parents[1] / 'transfer-runtime.py'))
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)

class Runtime(unittest.TestCase):
    def test_both_uids_run_isolated_and_cleanup(self):
        responses=[Mock(returncode=0,stdout='PASS: root-only transfer contract'),
                   Mock(returncode=0,stdout='PASS: non-root transfer denied'),
                   Mock(returncode=0),Mock(returncode=0)]
        with patch.object(runtime.subprocess,'run',side_effect=responses) as run:runtime.main()
        calls=run.call_args_list
        for call,uid in zip(calls,('0','1001')):
            argv=call.args[0]
            self.assertEqual(argv[argv.index('--user')+1],uid)
            self.assertEqual(argv[argv.index('--network')+1],'none')
            self.assertIn('--read-only',argv)
            self.assertNotIn('synthetic-transfer-probe',str(argv))
            self.assertIn('synthetic-transfer-probe',call.kwargs['input'])
        self.assertTrue(all(call.args[0][1]=='rm' for call in calls[2:]))

    def test_failed_or_timed_out_run_removes_named_container(self):
        for failure in (Mock(returncode=1,stdout='secret'),subprocess.TimeoutExpired('podman',90)):
            with patch.object(runtime.subprocess,'run',side_effect=[failure,Mock(returncode=0)]) as run:
                with self.assertRaises((RuntimeError,subprocess.TimeoutExpired)):runtime.main()
            self.assertEqual(run.call_count,2)
            first=run.call_args_list[0].args[0]
            self.assertEqual(run.call_args_list[1].args[0][-1],first[first.index('--name')+1])

    def test_cleanup_attempts_all_containers_after_first_removal_fails(self):
        for failure in (Mock(returncode=1),OSError('private-output'),subprocess.TimeoutExpired('podman',15)):
            responses=[Mock(returncode=0,stdout='PASS: root-only transfer contract'),
                       Mock(returncode=0,stdout='PASS: non-root transfer denied'),failure,Mock(returncode=0)]
            with patch.object(runtime.subprocess,'run',side_effect=responses) as run:
                with self.assertRaisesRegex(RuntimeError,'cleanup failed') as error:runtime.main()
            self.assertEqual(run.call_count,4)
            self.assertNotIn('private-output',str(error.exception))

if __name__=='__main__':unittest.main()
