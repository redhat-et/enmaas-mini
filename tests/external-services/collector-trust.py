#!/usr/bin/env python3
"""Exercise local collector trust without replacing running services or their CA.

Generates a disposable unrelated client CA in a private temporary directory.
Only empty telemetry batches are sent; no cloud credentials or real data are used.
"""
import importlib.util
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('trust_lab', Path(__file__).with_name('local.py'))
lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)


def main():
    request = urllib.request.Request('https://localhost:24443/v1/metrics',
        data=b'{"resourceMetrics":[]}', headers={'Content-Type': 'application/json'})
    # Establish availability and valid server/client trust first.
    with urllib.request.urlopen(request, context=lab.context(client=True), timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('valid client was not accepted')
    lab.require_tls_rejection(request, lab.context())
    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='enmaas-collector-trust-') as temporary:
            root = Path(temporary)
            openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=untrusted-test-CA', '-keyout', root/'ca.key', '-out', root/'ca.pem',
                    '-addext', 'basicConstraints=critical,CA:TRUE',
                    '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
            openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes',
                    '-subj', '/CN=untrusted-test-client', '-keyout', root/'client.key', '-out', root/'client.csr')
            (root/'client.ext').write_text('basicConstraints=critical,CA:FALSE\n'
                'keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n')
            openssl('x509', '-req', '-in', root/'client.csr', '-CA', root/'ca.pem',
                    '-CAkey', root/'ca.key', '-set_serial', '1', '-days', '1',
                    '-extfile', root/'client.ext', '-out', root/'client.pem')
            # Verify the negative fixture is a valid client chain under its own CA.
            openssl('verify', '-purpose', 'sslclient', '-CAfile', root/'ca.pem', root/'client.pem')
            foreign_client = lab.context()
            foreign_client.load_cert_chain(root/'client.pem', root/'client.key')
            lab.require_tls_rejection(request, foreign_client)
            wrong_server_ca = ssl.create_default_context(cafile=str(root/'ca.pem'))
            try:
                response = urllib.request.urlopen(request, context=wrong_server_ca, timeout=5)
            except ssl.SSLCertVerificationError:
                pass
            except urllib.error.URLError as error:
                if not isinstance(error.reason, ssl.SSLCertVerificationError):
                    raise RuntimeError('wrong-server-CA test did not prove certificate verification') from None
            else:
                response.close()
                raise RuntimeError('server was accepted under an unrelated CA')
    finally:
        os.umask(previous)
    # A collector crash must not explain the negative checks.
    with urllib.request.urlopen(request, context=lab.context(client=True), timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('valid client failed after negative probes')
    print('PASS: valid client before/after; absent and unrelated-CA clients rejected; server CA verified.')


if __name__ == '__main__':
    main()
