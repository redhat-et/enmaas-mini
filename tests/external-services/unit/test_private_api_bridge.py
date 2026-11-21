import sys
import unittest
from private_api_bridge import PrivateApiBridge

class PrivateBridge(unittest.TestCase):
    def test_multiple_calls_and_cleanup(self):
        bridge=PrivateApiBridge([sys.executable,'-u','-c','import sys,json\nfor line in sys.stdin: print(json.dumps({"ok":json.loads(line)["n"]}),flush=True)'])
        try:
            for i in range(3):self.assertEqual(bridge.call({'n':i}),{'ok':i})
        finally:bridge.close()
        self.assertIsNotNone(bridge.process.poll())

    def test_slow_and_invalid_reply_are_sanitized_and_process_stopped(self):
        for program in ('import time;time.sleep(20)', 'print("private-secret-value",flush=True)'):
            bridge=PrivateApiBridge([sys.executable,'-u','-c',program],timeout=.1)
            with self.assertRaisesRegex(RuntimeError,'Private management operation failed') as error:
                bridge.call({'n':1})
            self.assertNotIn('private-secret',str(error.exception))
            self.assertIsNotNone(bridge.process.poll())

    def test_large_command_is_rejected_before_write(self):
        bridge=PrivateApiBridge([sys.executable,'-u','-c','import time;time.sleep(20)'])
        try:
            with self.assertRaises(ValueError):bridge.call({'body':'x'*65536})
        finally:bridge.close()
