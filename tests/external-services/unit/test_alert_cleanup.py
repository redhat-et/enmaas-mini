"""Rule-test timeouts clean only their temporary container and secrets."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('alert_tests',Path(__file__).resolve().parents[1]/'test-alert-rules.py')
rules=importlib.util.module_from_spec(spec);spec.loader.exec_module(rules)


class Cleanup(unittest.TestCase):
    def test_cleanup_continues_with_bounded_owned_operations(self):
        with patch.object(rules.subprocess,'run',side_effect=[subprocess.TimeoutExpired('podman',10),Mock(returncode=1),Mock(returncode=0)]) as run:
            with self.assertRaisesRegex(RuntimeError,'cleanup incomplete'):
                rules.cleanup('owned',['owned-a','owned-b'])
        self.assertEqual(run.call_count,3)
        self.assertEqual(run.call_args_list[0].args[0],['podman','rm','--force','--ignore','owned'])
        self.assertEqual(run.call_args_list[-1].args[0],['podman','secret','rm','owned-a'])
        self.assertTrue(all(call.kwargs['timeout']==10 for call in run.call_args_list))


if __name__=='__main__':unittest.main()
