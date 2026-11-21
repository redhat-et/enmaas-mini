"""Exercise installer metadata checks before host mutations or Podman secrets."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('preflight_agent',HERE/'telemetry-agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)


class Preflight(unittest.TestCase):
    def test_private_files_and_size_bounds_before_host_changes(self):
        for installer in ('install-observability','install-telemetry-agent'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);inputs=root/'inputs';inputs.mkdir(mode=0o700)
                body=(HERE/installer).read_text()
                code=body.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
                code=code.replace('info.st_uid!=0','info.st_uid!=os.getuid()')
                if installer=='install-observability':
                    agent.observation.configure(inputs,'192.0.2.1','192.0.2.2','synthetic-client-secret','10.0.0.2')
                    names=('cert','key','issuer-ca','client-ca')
                    argv=['test',str(HERE/'observability-host.py'),str(inputs)]
                    tail=['192.0.2.2','10.0.0.1/32','192.0.2.10/32']
                else:
                    agent.configure(inputs,'10.0.0.2')
                    names=('ca','cert','key');argv=['test',str(inputs)];tail=[]
                for name in names:
                    file=root/name;file.write_text('synthetic');file.chmod(0o600)
                argv += [str(root/name) for name in names]+tail
                interfaces=json.dumps([{'addr_info':[{'family':'inet','local':'10.0.0.2'}]}])
                def check():
                    with patch.object(sys,'argv',list(argv)),patch('subprocess.check_output',return_value=interfaces),contextlib.redirect_stdout(io.StringIO()):
                        exec(compile(code,installer,'exec'),{'os':os})
                check()
                for file in (root/'key',inputs/'collector.yaml'):
                    original=file.read_bytes()
                    for size in (0,500001):
                        file.write_bytes(b'x'*size)
                        with self.assertRaisesRegex(SystemExit,'secret size'):check()
                    file.write_bytes(original)
                    file.chmod(0o644)
                    with self.assertRaisesRegex(SystemExit,'private regular'):check()
                    file.chmod(0o600)
                self.assertLess(body.index("<<'PY'"),body.index('create_secret()'))


if __name__=='__main__':unittest.main()
