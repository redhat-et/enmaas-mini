#!/usr/bin/env python3
"""Real OpenSSL verification of lab role bundles and trust separation."""
import importlib.util
from pathlib import Path
import subprocess
import ssl
import shlex
import sys
from unittest.mock import patch
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('certificates',(Path(__file__).resolve().parents[1] / 'certificates.py'))
certs=importlib.util.module_from_spec(spec);spec.loader.exec_module(certs)
spec=importlib.util.spec_from_file_location('certificate_transfer',(Path(__file__).resolve().parents[1] / 'transfer-client.py'))
transfer=importlib.util.module_from_spec(spec);spec.loader.exec_module(transfer)

def handshake(ca, certificate, key, hostname, *, client_ca=None, client_certificate=None, client_key=None):
    client_context=ssl.create_default_context(cafile=str(ca))
    server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate,key)
    if client_ca:
        server_context.verify_mode=ssl.CERT_REQUIRED
        server_context.load_verify_locations(cafile=str(client_ca))
    if client_certificate:
        client_context.load_cert_chain(client_certificate,client_key)
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
            # Exercise real generated PEM through the exact remote bundle writer.
            # Substitute only the UID and destination; no SSH/cloud connection.
            ssh_key=root/'ssh-key';ssh_key.write_text('fixture');ssh_key.chmod(0o600)
            known=root/'known-hosts';known.write_text('fixture');known.chmod(0o600)
            original_run=subprocess.run
            for role in transfer.BUNDLES:
                destination=root/(role+'-received')
                def remote(argv,**kwargs):
                    command=shlex.split(argv[-1]);script=command[4]
                    script=script.replace('os.geteuid()!=0','os.geteuid()!=os.getuid()').replace('s.st_uid!=0','s.st_uid!=os.getuid()')
                    return original_run([sys.executable,'-c',script,str(destination),command[-1]],**kwargs)
                with patch.object(transfer.subprocess,'run',side_effect=remote):
                    transfer.transfer_bundle('ec2-user@192.0.2.10',role,root/role,ssh_key,known)
                self.assertEqual(set(x.name for x in destination.iterdir()),set(transfer.BUNDLES[role]))
                for name in transfer.BUNDLES[role]:
                    self.assertEqual((destination/name).read_bytes(),(root/role/name).read_bytes())
                handshake(destination/'ca.pem',destination/'server.pem',destination/'server.key','localhost')
            client=root/'gateway/telemetry-client.pem'
            valid=subprocess.run(['openssl','verify','-CAfile',str(root/'trust/telemetry-ca.pem'),
                '-purpose','sslclient',str(client)],capture_output=True)
            self.assertEqual(valid.returncode,0)
            # Exercise generated RHEL role bundles through an actual TLS handshake,
            # not only certificate metadata or the local fixture's shared CA.
            server=(root/'trust/issuer-ca.pem',root/'observability/server.pem',
                    root/'observability/server.key','10.0.0.12')
            handshake(*server,client_ca=root/'trust/telemetry-ca.pem',
                      client_certificate=client,client_key=root/'gateway/telemetry-client.key')
            with self.assertRaises(ssl.SSLError):
                handshake(*server,client_ca=root/'trust/telemetry-ca.pem')
            with self.assertRaises(ssl.SSLError):
                handshake(*server,client_ca=root/'trust/telemetry-ca.pem',
                          client_certificate=root/'gateway/server.pem',client_key=root/'gateway/server.key')
            # Even trusting the HTTPS CA cannot turn a server-only leaf into a client.
            with self.assertRaises(ssl.SSLError):
                handshake(*server,client_ca=root/'trust/issuer-ca.pem',
                          client_certificate=root/'gateway/server.pem',client_key=root/'gateway/server.key')
            with self.assertRaises(ssl.SSLError):
                handshake(root/'trust/telemetry-ca.pem',client,root/'gateway/telemetry-client.key','10.0.0.10')
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
