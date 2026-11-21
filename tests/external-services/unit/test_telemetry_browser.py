"""CI browser must reach the private pod without enabling external ingress."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('telemetry_browser', HERE / 'telemetry-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class BrowserNetwork(unittest.TestCase):
    def test_rootful_linux_uses_pod_namespace(self):
        with patch.object(runtime.sys, 'platform', 'linux'), patch.object(runtime.os, 'geteuid', return_value=0), patch.object(runtime, 'command', return_value=b'false\n'):
            self.assertTrue(runtime.browser_namespace())

    def test_rootless_and_mac_keep_loopback(self):
        for platform, uid, rootless in [('linux', 1000, b'true'), ('linux', 0, b'true'), ('darwin', 501, b'false')]:
            with self.subTest(platform=platform, uid=uid, rootless=rootless), patch.object(runtime.sys, 'platform', platform), patch.object(runtime.os, 'geteuid', return_value=uid), patch.object(runtime, 'command', return_value=rootless):
                self.assertFalse(runtime.browser_namespace())

    def test_namespace_launch_never_uses_published_port(self):
        with patch.object(runtime, 'command', return_value=b'4321\n') as command:
            launch = runtime.browser_command('owned-infra', True, Path('/tmp/index'), Path('/tmp/evidence'))
        command.assert_called_once_with('inspect', '--format', '{{.State.Pid}}', 'owned-infra')
        self.assertEqual(launch[:6], ['nsenter', '--target', '4321', '--net', '--', 'node'])
        self.assertIn('http://127.0.0.1:8080', launch)

    def test_stopped_or_invalid_infra_pid_fails_closed(self):
        for pid in (b'0', b'1', b'-1', b'not-a-pid', b'42;echo injected'):
            with self.subTest(pid=pid), patch.object(runtime, 'command', return_value=pid):
                with self.assertRaisesRegex(RuntimeError, 'infra PID'):
                    runtime.browser_command('owned-infra', True, Path('/tmp/index'), Path('/tmp/evidence'))

    def test_rootless_launch_uses_only_loopback_mapping(self):
        with patch.object(runtime, 'command', return_value=b'127.0.0.1:45678\n'):
            launch = runtime.browser_command('owned-infra', False, Path('/tmp/index'), Path('/tmp/evidence'))
        self.assertEqual(launch[0], 'node')
        self.assertIn('http://127.0.0.1:45678', launch)

    def test_public_or_invalid_port_mapping_rejected(self):
        for mapping in (b'0.0.0.0:45678', b'127.0.0.1:0', b'127.0.0.1:99999', b'127.0.0.1:8\n0.0.0.0:9'):
            with self.subTest(mapping=mapping), patch.object(runtime, 'command', return_value=mapping):
                with self.assertRaisesRegex(RuntimeError, 'loopback'):
                    runtime.browser_command('owned-infra', False, Path('/tmp/index'), Path('/tmp/evidence'))


if __name__ == '__main__':
    unittest.main()
