"""Exercise cancellation using a real child interpreter and finally cleanup."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('security_runtime', (Path(__file__).resolve().parents[1] / 'security-runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeCleanupTests(unittest.TestCase):
    def test_redaction_secrets_use_pipe_input_for_native_podman_4(self):
        spec = importlib.util.spec_from_file_location('redaction_probe', (Path(__file__).resolve().parents[1] / 'redaction.py'))
        redaction = importlib.util.module_from_spec(spec);spec.loader.exec_module(redaction)
        class ProbeComplete(Exception):
            pass
        def certificates(root, *args, **kwargs):
            for name in ('observability/ca.pem', 'observability/server.pem', 'observability/server.key',
                         'observability/telemetry-ca.pem', 'gateway/telemetry-client.pem', 'gateway/telemetry-client.key'):
                (root/name).parent.mkdir(parents=True, exist_ok=True)
                (root/name).write_text('synthetic fixture')
        def command(args, **kwargs):
            self.assertEqual(args[:3], ['podman','secret','create'])
            self.assertNotIn('stdin', kwargs)
            self.assertIsInstance(kwargs.get('input'), bytes)
            self.assertTrue(kwargs['input'])
            raise ProbeComplete()
        with patch.object(redaction.certs,'generate',side_effect=certificates), patch.object(redaction.subprocess,'run',side_effect=command):
            with self.assertRaises(ProbeComplete):redaction.main()

    def test_ci_runtime_includes_disposable_provisioning_probe(self):
        with patch.object(runtime.subprocess,'run'),patch.object(runtime,'run_check') as checks:
            runtime.main()
        self.assertEqual(checks.call_args_list[0].args[0].name,'image-platforms.py')
        first=checks.call_args_list[1]
        self.assertEqual(first.args[0].name,'private-issuer-runtime.py')
        self.assertEqual(first.args[1],['--keycloak','--provisioning'])
        for name in ('issuer-routes-runtime.py','transfer-runtime.py'):
            self.assertIn(name,[call.args[0].name for call in checks.call_args_list])

    def test_timeout_unwinds_child_cleanup_and_remains_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'fixture.py'
            script.write_text('''import pathlib,sys,time
try:
    pathlib.Path(sys.argv[1]).write_text('started')
    time.sleep(60)
finally:
    pathlib.Path(sys.argv[2]).write_text('cleaned')
''')
            with self.assertRaises(subprocess.TimeoutExpired):
                runtime.run_check(script, [str(root/'started'), str(root/'cleaned')],
                                  timeout=2, cleanup_timeout=5)
            self.assertEqual((root/'started').read_text(), 'started')
            self.assertEqual((root/'cleaned').read_text(), 'cleaned')

    def test_assertion_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'fixture.py'
            script.write_text('raise SystemExit(7)')
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                runtime.run_check(script, [], timeout=5)
            self.assertEqual(failure.exception.returncode, 7)


if __name__ == '__main__':
    unittest.main()
