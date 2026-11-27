#!/usr/bin/env python3
"""Local lab safety and configuration contracts."""
import importlib.util
from pathlib import Path
import tempfile
from unittest.mock import patch, Mock
import json
import ssl
import urllib.error
import unittest
import yaml

spec = importlib.util.spec_from_file_location('local_lab', (Path(__file__).resolve().parents[1] / 'local.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)

class LocalLab(unittest.TestCase):
    def test_database_verifies_effective_mount_not_requested_size(self):
        for size in (65536000,536870912,0):
            with patch.object(lab.subprocess,'check_output',return_value=f'Filesystem Size Used Available Use% Mounted on\ntmpfs {size} 0 {size} 0% /dev/shm\n'):
                if size==536870912:lab.check_database_shm()
                else:
                    with self.assertRaisesRegex(RuntimeError,'shared-memory'):lab.check_database_shm()
        with patch.object(lab.subprocess,'check_output',return_value='invalid'):
            with self.assertRaisesRegex(RuntimeError,'Cannot verify'):lab.check_database_shm()

    def test_database_launch_isolates_and_bounds_shared_memory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);lab.configure(root,certificates=False)
            for name in ('ca.pem','server.pem','server.key'):(root/name).write_text('test fixture')
            def probe(args,**kwargs):
                return Mock(returncode=1 if args[1:3]==['pod','exists'] else 0)
            with patch.object(lab,'STATE',root),patch.object(lab,'command') as call, patch.object(lab.subprocess,'run',side_effect=probe), patch.object(lab.subprocess,'check_output',return_value=b'[{"RepoDigests":["example@sha256:abc"]}]'), patch.object(lab,'wait_url'),patch.object(lab,'check'),patch.object(lab,'check_database_shm'):
                lab.start()
            secrets=[c for c in call.call_args_list if c.args[0][1:3]==['secret','create']]
            self.assertTrue(secrets)
            for secret in secrets:
                self.assertNotIn('stdin',secret.kwargs)
                self.assertIsInstance(secret.kwargs.get('input'),bytes)
                self.assertTrue(secret.kwargs['input'])
            db=next(c.args[0] for c in call.call_args_list if '--name' in c.args[0] and lab.POD+'-db' in c.args[0])
            self.assertEqual(db[db.index('--ipc')+1],'private')
            self.assertEqual(db[db.index('--tmpfs')+1],'/dev/shm:rw,nosuid,nodev,noexec,size=512m,mode=1777')
            self.assertEqual(db[db.index('--memory')+1],'2g')
            self.assertIn(lab.POD+'-db:/var/lib/postgresql/data:U',db)
            self.assertIn('no-new-privileges',db)
            self.assertNotIn('--privileged',db)

    def test_trace_explorer_is_enabled_behind_existing_auth_proxy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lab.configure(root, certificates=False)
            config = yaml.safe_load((root/'perses.yaml').read_text())
            self.assertTrue(config['frontend']['explorer']['enable'])
            self.assertEqual(config['datasource']['proxy']['allowed_hosts'], ['127.0.0.1'])

    def test_inspected_ports_must_be_exact_loopback_bindings(self):
        ports={str(port)+'/tcp':[{'HostIp':'127.0.0.1','HostPort':str(port)}]
               for port in (18443,19443,24443)}
        for bindings in (ports,dict(ports,**{'28443/tcp':[{'HostIp':'127.0.0.1','HostPort':'28443'}]})):
            info={'Labels':{'io.enmaas.test-lab':lab.INSTANCE},'InfraConfig':{'PortBindings':bindings}}
            with patch.object(lab.subprocess,'check_output',return_value=json.dumps([info]).encode()):
                lab.check_port_bindings()
        import copy
        for kind in ('wildcard','ipv6','extra','missing','duplicate','remapped','foreign'):
            info={'Labels':{'io.enmaas.test-lab':lab.INSTANCE},'InfraConfig':{'PortBindings':copy.deepcopy(ports)}}
            bindings=info['InfraConfig']['PortBindings']
            if kind=='wildcard':bindings['18443/tcp'][0]['HostIp']='0.0.0.0'
            if kind=='ipv6':bindings['18443/tcp'][0]['HostIp']='::'
            if kind=='extra':bindings['5432/tcp']=[{'HostIp':'127.0.0.1','HostPort':'5432'}]
            if kind=='missing':bindings.pop('24443/tcp')
            if kind=='duplicate':bindings['18443/tcp']*=2
            if kind=='remapped':bindings['18443/tcp'][0]['HostPort']='443'
            if kind=='foreign':info['Labels']={}
            with self.subTest(kind=kind),patch.object(lab.subprocess,'check_output',return_value=json.dumps([info]).encode()):
                with self.assertRaises(RuntimeError):lab.check_port_bindings()

    def test_client_certificate_probe_requires_tls_rejection(self):
        request = Mock(); context = Mock()
        for error in (ssl.SSLError('certificate required'),
                      urllib.error.URLError(ssl.SSLError('certificate required'))):
            with patch.object(lab.urllib.request, 'urlopen', side_effect=error):
                lab.require_tls_rejection(request, context)
        for error in (urllib.error.URLError(TimeoutError()),
                      urllib.error.HTTPError('https://localhost',403,'forbidden',{},None),
                      ConnectionRefusedError()):
            with self.subTest(error=type(error).__name__), patch.object(lab.urllib.request,'urlopen',side_effect=error):
                with self.assertRaises(RuntimeError):lab.require_tls_rejection(request, context)
        with patch.object(lab.urllib.request,'urlopen') as opened:
            with self.assertRaises(RuntimeError):lab.require_tls_rejection(request, context)
            opened.return_value.close.assert_called_once()

    def test_all_local_signal_pipelines_apply_full_privacy_transform(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lab.configure(root, certificates=False)
            config = yaml.safe_load((root/'collector.yaml').read_text())
            self.assertEqual(config['receivers']['otlp']['protocols']['http']['max_request_body_size'],4194304)
            self.assertTrue(config['exporters']['prometheus']['send_timestamps'])
            self.assertEqual(config['processors']['batch']['send_batch_max_size'],512)
            for signal in ('logs', 'traces', 'metrics'):
                self.assertEqual(config['service']['pipelines'][signal]['processors'],
                                 ['memory_limiter', 'transform', 'batch'])
            transform = config['processors']['transform']
            self.assertIn('set(body, "gateway event")', str(transform))
            self.assertIn('set(links, nil)', str(transform))
            self.assertIn('metric_statements', transform)
            self.assertEqual(transform['error_mode'], 'propagate')

    def test_issuer_disables_self_registration_and_enables_temporary_lockout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lab.configure(root, certificates=False)
            realm = json.loads((root/'realm.json').read_text())
            self.assertFalse(realm['registrationAllowed'])
            self.assertFalse(realm['resetPasswordAllowed'])
            self.assertTrue(realm['bruteForceProtected'])
            self.assertFalse(realm['permanentLockout'])
            self.assertEqual(realm['failureFactor'], 5)
            self.assertGreaterEqual(realm['waitIncrementSeconds'], 60)
            self.assertGreater(realm['maxDeltaTimeSeconds'], realm['maxFailureWaitSeconds'])

    def test_gateway_oidc_uses_immutable_subject_and_separate_client(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lab.configure(root, certificates=False)
            realm = json.loads((root/'realm.json').read_text())
            clients = {c['clientId']: c for c in realm['clients']}
            gateway = clients['enmaas-mini']
            self.assertNotEqual(gateway['secret'], clients['observability']['secret'])
            self.assertEqual(gateway['redirectUris'], ['https://localhost:28443/oauth/callback'])
            self.assertEqual(gateway['attributes']['post.logout.redirect.uris'], 'https://localhost:28443/login')
            self.assertFalse(gateway['directAccessGrantsEnabled'])
            mapper = gateway['protocolMappers'][0]
            self.assertEqual(mapper['config']['user.attribute'], 'id')
            self.assertEqual(mapper['config']['claim.name'], 'rhatUUID')
            identities = json.loads((root/'identities.json').read_text())
            for user in realm['users']:
                self.assertEqual(identities[user['username']], user['id'])
            self.assertEqual(len(set(identities.values())), 3)
            config = json.loads((root/'gateway-oidc.json').read_text())
            self.assertEqual(config['client_id'], 'enmaas-mini')
            self.assertEqual(config['client_secret'], gateway['secret'])

    def test_management_client_is_explicit_local_test_only(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as d:
                root=Path(d)
                lab.configure(root, certificates=False, authorization_test=enabled)
                realm=json.loads((root/'realm.json').read_text())
                clients={c['clientId']:c for c in realm['clients']}
                self.assertEqual('lab-authorization-test' in clients, enabled)
                if enabled:
                    client=clients['lab-authorization-test']
                    self.assertTrue(client['serviceAccountsEnabled'])
                    self.assertEqual(client['defaultClientScopes'], ['kc-admin-api'])
                    self.assertFalse(client['standardFlowEnabled'])
                    self.assertFalse(client['directAccessGrantsEnabled'])
                    account=next(u for u in realm['users'] if u.get('serviceAccountClientId'))
                    self.assertEqual(account['clientRoles']['realm-management'], ['manage-users','query-groups','query-users'])
                    self.assertNotIn('realm-admin', str(account))

    def test_proxy_cookie_secret_is_urlsafe(self):
        with tempfile.TemporaryDirectory() as d, patch.object(lab.secrets, 'token_bytes', return_value=b'\xff'*32):
            root=Path(d)
            lab.configure(root,certificates=False)
            line=next(line for line in (root/'oauth.cfg').read_text().splitlines() if line.startswith('cookie_secret'))
            self.assertNotIn('/',line)
            self.assertNotIn('+',line)

    def test_images_are_digest_pinned(self):
        for image in lab.IMAGES.values():
            self.assertRegex(image, r'@sha256:[a-f0-9]{64}$')

    def test_cleanup_refuses_unowned_pod(self):
        with patch.object(lab.subprocess, 'run', return_value=Mock(returncode=0)), \
             patch.object(lab.subprocess, 'check_output', return_value=b'[ {"Labels": {}} ]'), \
             patch.object(lab, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'ownership label'):
                lab.down()
            command.assert_not_called()

    def test_cleanup_checks_secret_labels_instead_of_unreliable_ls_filter(self):
        with patch.object(lab.subprocess, 'run', return_value=Mock(returncode=1)), \
             patch.object(lab.subprocess, 'check_output', side_effect=[
                 'unrelated\n'+lab.POD+'-ca-pem\n',
                 json.dumps([{'Spec': {'Labels': {'io.enmaas.test-lab': 'external-local'}}}])]), \
             patch.object(lab, 'command') as command:
            lab.down()
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][-1], lab.POD+'-ca-pem')

    def test_public_bindings_are_loopback_tls_only(self):
        self.assertEqual(lab.PORTS, (18443, 19443, 24443))
        self.assertIn("127.0.0.1:28443:28443", lab.pod_args(gateway=True))
        self.assertNotIn("127.0.0.1:28443:28443", lab.pod_args())
        args = lab.pod_args()
        for port in lab.PORTS:
            self.assertIn(f'127.0.0.1:{port}:{port}', args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('--network=host', args)

    def test_oidc_requires_explicit_group_and_verified_tls(self):
        with tempfile.TemporaryDirectory() as d:
            lab.configure(Path(d), certificates=False)
            proxy = (Path(d)/'oauth.cfg').read_text()
            self.assertIn('allowed_groups = ["/observability-admin"]', proxy)
            self.assertIn('provider_ca_files = ["/lab/ca.pem"]', proxy)
            self.assertNotIn('skip_verify', proxy)
            self.assertIn('code_challenge_method = "S256"', proxy)
            self.assertEqual((Path(d)/'credentials.json').stat().st_mode & 0o777, 0o600)

    def test_machine_ingestion_requires_client_certificate(self):
        with tempfile.TemporaryDirectory() as d:
            lab.configure(Path(d), certificates=False)
            collector = (Path(d)/'collector.yaml').read_text()
            self.assertIn('client_ca_file: /lab/ca.pem', collector)
            self.assertIn('endpoint: 0.0.0.0:24443', collector)
            self.assertIn('memory_limiter', collector)
            self.assertEqual(yaml.safe_load(collector)['processors']['transform'], lab.privacy_transform())

    def test_repeat_configuration_does_not_rotate_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            lab.configure(Path(d), certificates=False)
            before = (Path(d)/'credentials.json').read_bytes()
            with self.assertRaises(FileExistsError):
                lab.configure(Path(d), certificates=False)
            self.assertEqual(before, (Path(d)/'credentials.json').read_bytes())

if __name__ == '__main__':
    unittest.main()
