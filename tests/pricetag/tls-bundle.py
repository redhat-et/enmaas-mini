#!/usr/bin/env python3
"""Verify externally prepared lab TLS without deploying CA signing keys."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/pricetag'))
import tls_bundle
spec=importlib.util.spec_from_file_location('certs',ROOT/'tests/external-services/certificates.py')
certs=importlib.util.module_from_spec(spec);spec.loader.exec_module(certs)

class Bundle(unittest.TestCase):
    def test_valid_bundle_bad_hostname_key_permissions_and_ca(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'certs'
            certs.generate(root,{r:['192.0.2.10'] for r in certs.ROLES},14)
            bundle=root/'gateway'
            tls_bundle.validate(bundle,'192.0.2.10')
            with self.assertRaises(ValueError):tls_bundle.validate(bundle,'192.0.2.99')
            bundle.chmod(0o777)
            with self.assertRaises(ValueError):tls_bundle.validate(bundle,'192.0.2.10')
            bundle.chmod(0o700)
            (bundle/'server.key').chmod(0o644)
            with self.assertRaises(ValueError):tls_bundle.validate(bundle,'192.0.2.10')
            (bundle/'server.key').chmod(0o600)
            (bundle/'ca.pem').write_bytes((root/'trust/telemetry-ca.pem').read_bytes())
            with self.assertRaises(ValueError):tls_bundle.validate(bundle,'192.0.2.10')

if __name__=='__main__':unittest.main()
