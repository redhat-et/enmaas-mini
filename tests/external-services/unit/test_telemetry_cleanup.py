"""Fixture failures must not skip later owned-resource cleanup."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock,patch

HERE=Path(__file__).resolve().parents[1]


class Cleanup(unittest.TestCase):
    def test_all_resources_attempted_after_failure_or_timeout(self):
        for name in ('redaction','telemetry-runtime'):
            spec=importlib.util.spec_from_file_location('cleanup_'+name,HERE/(name+'.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            with patch.object(module.subprocess,'run',side_effect=[subprocess.TimeoutExpired('podman',10),Mock(returncode=1),Mock(returncode=0)]) as run:
                with self.assertRaisesRegex(RuntimeError,'cleanup incomplete'):
                    module.cleanup('owned',['secret-a','secret-b'],True)
            self.assertEqual(run.call_count,3)
            self.assertEqual(run.call_args_list[-1].args[0],['podman','secret','rm','secret-a'])
            self.assertTrue(all(call.kwargs['timeout']<=15 for call in run.call_args_list))


if __name__=='__main__':unittest.main()
