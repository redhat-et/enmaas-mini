"""Exercise cancellation using a real child interpreter and finally cleanup."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('security_runtime', Path(__file__).with_name('security-runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeCleanupTests(unittest.TestCase):
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
