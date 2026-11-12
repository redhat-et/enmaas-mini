"""The local telemetry relay needs bounded writable receiver state."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('gateway_signals', Path(__file__).with_name('gateway-signals.py'))
signals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signals)

class RelayRuntime(unittest.TestCase):
    def test_offset_state_is_writable_and_bounded_without_host_mounts(self):
        args = signals.relay_runtime_options()
        self.assertIn('--read-only', args)
        self.assertIn('--cap-drop', args)
        self.assertIn('/positions:rw,noexec,nosuid,size=16m,mode=1777', args)
        self.assertNotIn('--volume', args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('--publish', args)

if __name__ == '__main__':
    unittest.main()
