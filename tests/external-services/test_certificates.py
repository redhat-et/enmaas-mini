#!/usr/bin/env python3
"""Real OpenSSL verification of lab role bundles and trust separation."""
import importlib.util
from pathlib import Path
import subprocess
import ssl
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('certificates',Path(__file__).with_name('certificates.py'))
certs=importlib.util.module_from_spec(spec);spec.loader.exec_module(certs)

def handshake(ca, certificate, key, hostname):
    client_context=ssl.create_default_context(cafile=str(ca))
    server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate,key)
    incoming,outgoing=ssl.MemoryBIO(),ssl.MemoryBIO()
    server_in,server_out=ssl.MemoryBIO(),ssl.MemoryBIO()
    client=client_context.wrap_bio(incoming,outgoing,server_hostname=hostname)
    server=server_context.wrap_bio(server_in,server_out,server_side=True)
    done=set()
    for _ in range(20):
        for label,endpoint in (('client',client),('server',server)):
            try:endpoint.do_handshake();done.add(label)
            except ssl.SSLWantReadError:pass
        if outgoing.pending:server_in.write(outgoing.read())
        if server_out.pending:incoming.write(server_out.read())
        if len(done)==2:return
    raise AssertionError('TLS handshake did not finish')


class Certificates(unittest.TestCase):
    def test_role_bundles_and_eku_isolate_client_trust(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'lab'
            certs.generate(root,{'gateway':['192.0.2.10','10.0.0.10'],
                                 'keycloak':['192.0.2.11','10.0.0.11'],
                                 'observability':['192.0.2.12','10.0.0.12']},7)
            for role in ('gateway','keycloak','observability'):
                bundle=root/role
                result=subprocess.run(['openssl','verify','-CAfile',str(root/'trust/issuer-ca.pem'),
                    '-purpose','sslserver',str(bundle/'server.pem')],capture_output=True)
                self.assertEqual(result.returncode,0)
                self.assertFalse(any(p.name.endswith('ca.key') for p in bundle.rglob('*')))
                self.assertEqual((bundle/'server.key').stat().st_mode&0o777,0o600)
            client=root/'gateway/telemetry-client.pem'
            valid=subprocess.run(['openssl','verify','-CAfile',str(root/'trust/telemetry-ca.pem'),
                '-purpose','sslclient',str(client)],capture_output=True)
            self.assertEqual(valid.returncode,0)
            wrong=subprocess.run(['openssl','verify','-CAfile',str(root/'trust/telemetry-ca.pem'),
                '-purpose','sslclient',str(root/'keycloak/server.pem')],capture_output=True)
            self.assertNotEqual(wrong.returncode,0)
            for path in root.rglob('*'):
                self.assertEqual(path.stat().st_mode&0o077,0)
            with self.assertRaises(FileExistsError):certs.generate(root,{},7)

    def test_optional_dns_identity_is_signed_only_for_selected_role(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'lab'
            certs.generate(root,{'gateway':['192.0.2.10'], 'keycloak':['192.0.2.11'],
                                 'observability':['192.0.2.12']},7,
                           dns_names={'keycloak':'identity.lab.example.com'})
            ca=root/'trust/issuer-ca.pem'
            handshake(ca,root/'keycloak/server.pem',root/'keycloak/server.key','identity.lab.example.com')
            with self.assertRaises(ssl.SSLCertVerificationError):
                handshake(ca,root/'keycloak/server.pem',root/'keycloak/server.key','other.lab.example.com')
            with self.assertRaises(ssl.SSLCertVerificationError):
                handshake(ca,root/'gateway/server.pem',root/'gateway/server.key','identity.lab.example.com')

    def test_dns_name_input_rejects_injection_wildcards_and_unknown_roles(self):
        hosts={'gateway':['192.0.2.10'],'keycloak':['192.0.2.11'],'observability':['192.0.2.12']}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'lab'
            for names in ({'other':'lab.example.com'}, {'keycloak':'*.example.com'},
                          {'keycloak':'192.0.2.11'}, {'keycloak':'host'},
                          {'keycloak':'host.example.com\nkeyUsage=keyCertSign'},
                          {'keycloak':'https://host.example.com'}, {'keycloak':'host.example.com,IP:1.1.1.1'}):
                with self.subTest(names=names),self.assertRaises(ValueError):
                    certs.generate(root,hosts,7,dns_names=names)
                self.assertFalse(root.exists())

    def test_invalid_inputs_do_not_create_output(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'lab'
            for hosts,days in (({'gateway':['bad']},7),({},0),({},365)):
                with self.assertRaises(ValueError):certs.generate(root,hosts,days)
                self.assertFalse(root.exists())

if __name__=='__main__':unittest.main()
