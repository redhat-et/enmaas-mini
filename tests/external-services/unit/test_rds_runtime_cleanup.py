"""Disposable DB checks must attempt every cleanup after errors/timeouts."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'rds_runtime_cleanup', Path(__file__).resolve().parents[1] / 'rds-bootstrap-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class Cleanup(unittest.TestCase):
    def test_readiness_waits_for_final_tcp_server(self):
        command = Mock(side_effect=[Mock(returncode=2), Mock(returncode=0)])
        with patch.object(runtime.time, 'sleep') as sleep:
            runtime.wait_database(command, 'owned-db')
        self.assertEqual(command.call_count, 2)
        # The image's temporary initialization server accepts Unix sockets only.
        command.assert_called_with(
            ['exec', 'owned-db', 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'], check=False)
        sleep.assert_called_once_with(0.5)

    def test_readiness_timeout_is_bounded_and_redacted(self):
        command = Mock(return_value=Mock(returncode=2, stderr='secret-sentinel'))
        with patch.object(runtime.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'readiness timeout') as error:
            runtime.wait_database(command, 'owned-db')
        self.assertEqual(command.call_count, 60)
        self.assertNotIn('secret-sentinel', str(error.exception))

    def test_cleanup_is_bounded_and_attempts_secret_after_container_failure(self):
        for failure in (Mock(returncode=1), OSError('secret-sentinel'),
                        subprocess.TimeoutExpired('podman', 30)):
            with patch.object(runtime.subprocess, 'run', side_effect=[failure, Mock(returncode=0)]) as run:
                with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete') as error:
                    runtime.cleanup('owned-db', 'owned-secret', container=True, secret=True)
            self.assertNotIn('secret-sentinel', str(error.exception))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0],
                             ['podman', 'rm', '--force', '--volumes', '--ignore', 'owned-db'])
            self.assertEqual(run.call_args_list[1].args[0], ['podman', 'secret', 'rm', 'owned-secret'])
            self.assertTrue(all(call.kwargs['timeout'] == 30 for call in run.call_args_list))

    def test_only_attempted_resources_are_removed(self):
        with patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            runtime.cleanup('owned-db', 'owned-secret', container=False, secret=False)
            run.assert_not_called()
            runtime.cleanup('owned-db', 'owned-secret', container=False, secret=True)
            self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
