"""Parallel local qualification must not alter another fixture's listeners."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('parallel_lab', HERE/'local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Endpoints(unittest.TestCase):
    def test_parallel_fixture_has_distinct_loopback_listeners_and_redirects(self):
        with patch.dict(os.environ, ENMAAS_LAB_PORT_OFFSET='1000'):
            lab = load()
            self.assertEqual(lab.url('gateway'), 'https://localhost:29443')
            self.assertIn('127.0.0.1:29443:29443', lab.pod_args(gateway=True))
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lab.configure(root, certificates=False)
                self.assertIn('https-port=19443', (root/'keycloak.conf').read_text())
                self.assertIn('https://localhost:20443', (root/'oauth.cfg').read_text())
                self.assertIn('https://localhost:29443/oauth/callback', (root/'realm.json').read_text())
                self.assertEqual(json.loads((root/'endpoints.json').read_text())['offset'], 1000)
                with patch.object(lab, 'STATE', root):
                    lab.check_endpoints()
                    document=json.loads((root/'endpoints.json').read_text())
                    document['offset']=0
                    (root/'endpoints.json').write_text(json.dumps(document))
                    with self.assertRaisesRegex(ValueError, 'offset'):
                        lab.check_endpoints()

    def test_offset_is_bounded_and_shared_by_browser_clients(self):
        for value in ('-1', '30001', '1.5', ' 1', 'secret', ''):
            with self.subTest(value=value), patch.dict(os.environ, ENMAAS_LAB_PORT_OFFSET=value):
                with self.assertRaises(ValueError):
                    load()
        with patch.dict(os.environ, ENMAAS_LAB_PORT_OFFSET='1000'):
            result = subprocess.check_output(['node', '-e',
                "console.log(require('./tests/external-services/local-endpoints.cjs').url('gateway'))"],
                cwd=HERE.parents[1], text=True)
            self.assertEqual(result.strip(), 'https://localhost:29443')


if __name__ == '__main__':
    unittest.main()
