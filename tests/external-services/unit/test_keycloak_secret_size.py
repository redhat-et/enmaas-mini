"""Reject oversized realm secrets before changing the RHEL host or creating secrets."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parents[1]


class SecretSize(unittest.TestCase):
    def test_preflight_bounds_each_podman_secret_before_any_host_mutation(self):
        installer=(HERE/'install-keycloak').read_text()
        code=installer.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        # Run the actual metadata preflight under the CI user's UID.
        code=code.replace('info.st_uid!=0','info.st_uid!=os.getuid()')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'quadlets').mkdir(mode=0o700)
            names=('db-password','keycloak.conf','realm.json','proxy.json','cert.pem','key.pem','ca.pem')
            for name in names:
                p=root/name;p.write_bytes(b'x');p.chmod(0o600)
            args=[sys.executable,'-c',code,str(root),str(root/'cert.pem'),str(root/'key.pem'),str(root/'ca.pem'),'192.0.2.1/32']
            self.assertEqual(subprocess.run(args,capture_output=True).returncode,0)
            for name in names[:-1]:
                path=root/name
                for size in (0,500001):
                    with self.subTest(name=name,size=size):
                        path.write_bytes(b'x'*size)
                        result=subprocess.run(args,capture_output=True)
                        self.assertNotEqual(result.returncode,0)
                        self.assertIn(b'secret',result.stderr)
                path.write_bytes(b'x')
            path=root/'realm.json';path.unlink();path.mkdir(mode=0o700)
            self.assertNotEqual(subprocess.run(args,capture_output=True).returncode,0)
            path.rmdir()
            (root/'realm.json').write_bytes(b'x'*500000)
            (root/'realm.json').chmod(0o600)
            self.assertEqual(subprocess.run(args,capture_output=True).returncode,0)
        self.assertLess(installer.index("<<'PY'"),installer.index('dnf install'))
        self.assertLess(installer.index("<<'PY'"),installer.index('create_secret()'))


if __name__=='__main__':unittest.main()
