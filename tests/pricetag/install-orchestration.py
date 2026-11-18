#!/usr/bin/env python3
"""Exercise the real install orchestrator; replace only host/service boundaries."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('install_orchestration_subject', ROOT/'scripts/pricetag/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Pipeline(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.units = self.home/'units'
        self.units.mkdir()
        self.inputs = self.home/'inputs'
        self.inputs.mkdir()
        self.commands = []
        self.output = io.StringIO()
        self.failure = None
        self.volume_status = 1
        self.helper = SimpleNamespace(
            STATE=self.inputs, save_provider=Mock(),
            provider_urls=Mock(return_value={'openai': 'https://provider.example/v1'}),
            collect_credential=Mock(return_value='synthetic-private-credential'),
            provider_settings=Mock(return_value={'models': []}),
        )
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        patches = {
            'preflight': Mock(), 'provider_module': Mock(return_value=self.helper),
            'open_log': Mock(), 'service_prefix': Mock(return_value=['service-user']),
            'acquire_images': Mock(return_value=('maas-image', 'metering-image')),
            'import_image': Mock(side_effect=['sha256:'+'1'*64, 'sha256:'+'2'*64, 'sha256:'+'3'*64]),
            'run': self.run_command, 'INPUTS': self.inputs, 'DEPLOY': self.home/'config',
            'Path': lambda value: self.units if str(value).startswith('/etc/containers/systemd/users/') else Path(value),
        }
        for name, value in patches.items():
            self.stack.enter_context(patch.object(installer, name, value))
        self.stack.enter_context(patch.object(installer.os, 'umask'))
        self.stack.enter_context(patch.object(installer.platform, 'machine', return_value='x86_64'))
        self.stack.enter_context(patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=995)))
        self.stack.enter_context(patch.object(installer.subprocess, 'run', side_effect=self.host_command))
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    def run_command(self, command, *args, **kwargs):
        command = list(map(str, command))
        self.commands.append(command)
        if self.failure and self.failure(command):
            raise subprocess.CalledProcessError(1, command)
        return ''

    def host_command(self, command, **kwargs):
        if command == ['service-user', 'podman', 'volume', 'exists', 'pricetag-data']:
            return SimpleNamespace(returncode=self.volume_status)
        if command == ['service-user', 'podman', 'secret', 'create', 'pricetag-mock-ui-v1', '-']:
            self.assertEqual(kwargs['input'], b'synthetic-anthropic')
            self.assertTrue(kwargs['check'])
            return SimpleNamespace(returncode=0)
        self.fail('Unexpected host command: ' + str(command))

    def args(self, *extra):
        args = installer.parse_args(['--hostname', 'gateway.example', '--mode', 'mock',
                                     '--plan', '--yes', *extra])
        args.plan = False
        return args

    def preparation(self):
        return next(c for c in self.commands if str(ROOT/'scripts/pricetag/prepare.py') in c)

    def test_mock_profiles_create_replaceable_inputs_and_verify_tls(self):
        installer.install(self.args('--no-sample-users'))
        command = self.preparation()
        for flag in ('--mock-provider', '--mock-profiles', '--provider-unit', '--price-sources', '--no-sample-users'):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--praxis-image')+1], 'sha256:'+'1'*64)
        self.assertEqual({c.args[0] for c in self.helper.save_provider.call_args_list}, {'openai', 'external'})
        self.assertEqual({m['provider'] for m in json.loads((self.inputs/'models.json').read_text())}, {'openai', 'external'})
        for file in self.inputs.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.commands[-1][0], 'curl')
        self.assertIn('--cacert', self.commands[-1])
        self.assertNotIn('--insecure', self.commands[-1])
        self.assertIn('Installation complete', self.output.getvalue())

    def test_real_providers_export_without_mock_and_hide_credentials(self):
        args = self.args('--mode', 'real', '--provider', 'openai', '--provider', 'external',
                         '--external-api', 'both', '--external-messages-auth', 'bearer')
        installer.install(args)
        self.assertEqual(self.helper.save_provider.call_count, 2)
        self.assertEqual(self.helper.MESSAGES_AUTH, 'bearer')
        self.assertIn([str(installer.sys.executable), str(ROOT/'scripts/pricetag/providers'), 'export'], self.commands)
        command = self.preparation()
        self.assertIn('--providers', command)
        self.assertIn('--models', command)
        self.assertNotIn('--mock-provider', command)
        self.assertNotIn('synthetic-private-credential', self.output.getvalue()+str(self.commands))
        self.assertNotIn('Benchmark:', self.output.getvalue())

    def test_legacy_demo_creates_private_mock_secret(self):
        installer.install(self.args('--demo-fixture'))
        command = self.preparation()
        self.assertIn('--managed-mock', command)
        self.assertIn('--mock-secret', command)
        self.assertNotIn('--mock-profiles', command)
        self.helper.save_provider.assert_not_called()

    def test_remote_service_inputs_are_validated_and_forwarded(self):
        # Validators have their own contract suites; here test orchestration/order.
        installer.sys.path.insert(0, str(ROOT/'scripts/pricetag'))
        import prepare
        import oidc_config
        import tls_bundle
        args = self.args('--image-mode', 'source', '--no-sample-users')
        args.maas_database_url_file = self.home/'maas.url'
        args.metering_database_url_file = self.home/'metering.url'
        args.external_database_ca_file = self.home/'db-ca.pem'
        args.oidc_config = self.home/'oidc.json'
        args.tls_bundle = self.home/'tls'
        args.maas_database_url_file.write_text('synthetic-maas-db')
        args.metering_database_url_file.write_text('synthetic-metering-db')
        with patch.object(prepare, 'validate_external_database_urls') as db, patch.object(
                oidc_config, 'load') as oidc, patch.object(tls_bundle, 'validate') as tls:
            self.volume_status = 125  # External DB must not query a local data volume.
            installer.install(args)
            db.assert_called_once_with('synthetic-maas-db', 'synthetic-metering-db', args.external_database_ca_file)
            oidc.assert_called_once_with(args.oidc_config, 'https://gateway.example:8443')
            tls.assert_called_once_with(args.tls_bundle, args.hostname)
        command = self.preparation()
        for flag, value in (('--maas-database-url-file', args.maas_database_url_file),
                            ('--metering-database-url-file', args.metering_database_url_file),
                            ('--external-database-ca-file', args.external_database_ca_file),
                            ('--oidc-config', args.oidc_config), ('--tls-bundle', args.tls_bundle)):
            self.assertEqual(command[command.index(flag)+1], str(value))
        self.assertNotIn('synthetic-maas-db', self.output.getvalue()+str(self.commands))

    def test_missing_database_file_stops_before_host_mutation(self):
        args = self.args()
        args.maas_database_url_file = self.home/'missing.url'
        with self.assertRaises(ValueError):
            installer.install(args)
        installer.preflight.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_external_database_can_use_system_trust_without_ca_override(self):
        installer.sys.path.insert(0, str(ROOT/'scripts/pricetag'))
        import prepare
        args = self.args()
        args.maas_database_url_file = self.home/'maas.url'
        args.metering_database_url_file = self.home/'metering.url'
        for path in (args.maas_database_url_file, args.metering_database_url_file):
            path.write_text('synthetic-db-url')
        with patch.object(prepare, 'validate_external_database_urls') as validate:
            installer.install(args)
        validate.assert_called_once_with('synthetic-db-url', 'synthetic-db-url', None)
        self.assertNotIn('--external-database-ca-file', self.preparation())

    def test_unsupported_architecture_stops_before_packages(self):
        with patch.object(installer.platform, 'machine', return_value='unsupported'):
            with self.assertRaisesRegex(ValueError, 'supported architectures'):
                installer.install(self.args())
        self.assertEqual(self.commands, [])

    def test_provider_persistence_failure_prevents_configuration_and_start(self):
        args = self.args('--mode', 'real', '--provider', 'openai')
        self.helper.save_provider.side_effect = ValueError('secret creation failed')
        with self.assertRaisesRegex(ValueError, 'secret creation failed'):
            installer.install(args)
        self.assertFalse(any(str(ROOT/'scripts/pricetag/prepare.py') in c for c in self.commands))
        self.assertFalse(any(str(ROOT/'scripts/pricetag/start') in c for c in self.commands))

    def test_refuse_existing_volume_and_storage_errors_before_images(self):
        for status in (0, 2, 125):
            with self.subTest(status=status):
                self.volume_status = status
                with self.assertRaisesRegex(ValueError, 'existing data volume or storage error'):
                    installer.install(self.args())
                installer.acquire_images.assert_not_called()
                self.helper.save_provider.assert_not_called()

    def test_refuse_retained_quadlets_before_images(self):
        (self.units/'pricetag-db.container').write_text('[Container]\n')
        with self.assertRaisesRegex(ValueError, 'existing PriceTag Quadlets'):
            installer.install(self.args())
        installer.acquire_images.assert_not_called()

    def test_failed_stages_stop_without_cleanup_or_success(self):
        stages = ('dnf', str(ROOT/'scripts/common/install'),
                  str(ROOT/'scripts/pricetag/prepare.py'), str(ROOT/'scripts/pricetag/start'), 'curl')
        for stage in stages:
            with self.subTest(stage=stage):
                self.commands.clear()
                self.output.seek(0)
                self.output.truncate()
                installer.import_image.side_effect = ['sha256:'+'1'*64, 'sha256:'+'2'*64, 'sha256:'+'3'*64]
                self.failure = lambda command: stage in command
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.install(self.args('--demo-fixture'))
                self.assertIn(stage, self.commands[-1])
                self.assertNotIn('Installation complete', self.output.getvalue())
                self.assertFalse(any('rm' in c or 'prune' in c for c in self.commands))

    def test_import_failure_does_not_persist_providers_or_prepare(self):
        for index in range(3):
            with self.subTest(image=index):
                installer.import_image.side_effect = ['sha256:'+'1'*64]*index + [ValueError('import failed')]
                with self.assertRaisesRegex(ValueError, 'import failed'):
                    installer.install(self.args())
                self.helper.save_provider.assert_not_called()
                self.assertFalse(any(str(ROOT/'scripts/pricetag/prepare.py') in c for c in self.commands))


class SourceCache(unittest.TestCase):
    def test_fresh_checkout_fetches_exact_revision(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(installer, 'run') as run:
            directory = Path(tmp)/'source'
            installer.checkout('https://example.org/project.git', directory, 'a'*40)
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ['git', 'clone', 'https://example.org/project.git', directory],
                ['git', '-C', directory, 'fetch', 'origin', 'a'*40],
                ['git', '-C', directory, 'checkout', '--detach', 'FETCH_HEAD'],
            ])

    def test_existing_cache_requires_git_directory_correct_origin_and_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/'source'
            directory.mkdir()
            with patch.object(installer, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'invalid source cache'):
                    installer.checkout('expected', directory)
                run.assert_not_called()
            (directory/'.git').mkdir()
            for responses, error in ((['unexpected'], 'unexpected origin'),
                                     (['expected', ' M tracked-file'], 'dirty')):
                with self.subTest(error=error), patch.object(installer, 'run', side_effect=responses) as run:
                    with self.assertRaisesRegex(RuntimeError, error):
                        installer.checkout('expected', directory, 'a'*40)
                    self.assertFalse(any('checkout' in c.args[0] or 'fetch' in c.args[0] for c in run.call_args_list))
            with patch.object(installer, 'run', side_effect=['expected', '']) as run:
                installer.checkout('expected', directory)
                self.assertEqual(run.call_count, 2)
            link = Path(tmp)/'link'
            link.symlink_to(directory, target_is_directory=True)
            with patch.object(installer, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'invalid source cache'):
                    installer.checkout('expected', link)
                run.assert_not_called()


class ArgumentBoundaries(unittest.TestCase):
    def test_external_plan_defaults_protocols_but_headless_requires_them(self):
        base = ['--hostname', 'gateway.example', '--provider', 'external']
        self.assertEqual(installer.parse_args(base+['--plan']).external_api, 'both')
        with patch.object(installer.sys.stdin, 'isatty', return_value=False), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            installer.parse_args(base+['--yes'])
        args = installer.parse_args(['--hostname', 'gateway.example', '--mode', 'mock', '--image-mode', 'source', '--plan'])
        with patch.object(installer, 'preflight') as preflight, contextlib.redirect_stdout(io.StringIO()):
            installer.install(args)
        preflight.assert_not_called()
    def test_invalid_combinations_fail_before_any_host_operation(self):
        base = ['--hostname', 'gateway.example', '--plan', '--provider', 'openai']
        cases = [
            (['--monthly-usd', '0'], 'monthly-usd'),
            (['--maas-database-url-file', 'relative', '--metering-database-url-file', '/root/db'], 'absolute'),
            (['--maas-database-url-file', '/root/a', '--metering-database-url-file', '/root/b', '--external-database-ca-file', 'relative'], 'absolute'),
            (['--external-database-ca-file', '/root/ca'], 'requires both'),
            (['--provider', 'external', '--external-api', 'openai', '--external-messages-url', 'https://example.org'], 'Messages URL'),
            (['--provider', 'external', '--external-api', 'anthropic', '--external-openai-url', 'https://example.org'], 'OpenAI URL'),
            (['--openai-key-file', 'relative'], 'absolute'),
            (['--external-key-file', '/root/key'], 'requires that provider'),
            (['--external-openai-url', 'https://example.org'], 'URLs require'),
            (['--external-api', 'both'], 'requires --provider'),
            (['--provider', 'pricetag', '--pricetag-key-file', '/root/key'], 'requires a PriceTag URL'),
            (['--maas-revision', 'short'], 'full lowercase'),
            (['--metering-revision', 'A'*40], 'full lowercase'),
            (['--image-mode', 'source', '--maas-image', 'registry.example/image@sha256:'+'a'*64], 'published'),
        ]
        with patch.object(installer, 'preflight') as preflight, patch.object(installer, 'run') as run:
            for flags, message in cases:
                output = io.StringIO()
                with self.subTest(flags=flags), contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
                    installer.parse_args(base+flags)
                self.assertIn(message, output.getvalue())
            preflight.assert_not_called()
            run.assert_not_called()

    def test_headless_external_requires_urls_for_each_selected_api(self):
        base = ['--hostname', 'gateway.example', '--provider', 'external', '--yes',
                '--non-interactive', '--external-key-file', '/root/key']
        for api in ('both', 'anthropic', 'openai'):
            with self.subTest(api=api), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                installer.parse_args(base+['--external-api', api])
        with patch.object(installer.sys.stdin, 'isatty', return_value=False), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            installer.parse_args(['--hostname', 'gateway.example', '--provider', 'openai'])

    def test_interactive_hostname_and_retry_selection(self):
        with patch.object(installer.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=' gateway.example '):
            args = installer.parse_args(['--provider', 'openai', '--yes'])
            self.assertEqual(args.hostname, 'gateway.example')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['invalid', '99', '2']), contextlib.redirect_stdout(output):
            self.assertEqual(installer.choice('Choose', {'a': 'First', 'b': 'Second'}, 'a'), 'b')
        self.assertEqual(output.getvalue().count('Choose one of:'), 2)

    def test_external_api_selection_and_explicit_image_defaults(self):
        with patch.object(installer.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='anthropic'), contextlib.redirect_stdout(io.StringIO()):
            args = installer.parse_args(['--hostname', 'gateway.example', '--provider', 'external', '--yes'])
            self.assertEqual(args.external_api, 'anthropic')
        image = 'registry.example/project/image@sha256:'+'a'*64
        args = installer.parse_args(['--hostname', 'gateway.example', '--mode', 'mock', '--plan', '--maas-image', image])
        self.assertEqual(args.image_mode, 'published')
        self.assertEqual(args.maas_image, image)
        for flags in ([], ['--image-mode', 'published', '--maas-branch', 'example']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                installer.parse_args(['--plan', '--mode', 'mock', *flags])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            installer.parse_args(['--plan', '--mode', 'mock', '--hostname', 'gateway.example',
                                  '--image-mode', 'published', '--maas-branch', 'example'])


class HostHelpers(unittest.TestCase):
    def test_cli_closes_log_and_reports_private_path_on_failure(self):
        for error in (None, ValueError('stage failed'), KeyboardInterrupt(), EOFError()):
            with self.subTest(error=type(error).__name__):
                log = io.StringIO()
                output = io.StringIO()
                with patch.object(installer, 'main', side_effect=error), patch.object(installer, 'LOG', log), patch.object(installer, 'LOG_PATH', Path('/var/log/synthetic-install.log')), contextlib.redirect_stderr(output):
                    if error is None:
                        installer.cli()
                        self.assertEqual(output.getvalue(), '')
                    else:
                        with self.assertRaises(SystemExit) as result:
                            installer.cli()
                        self.assertEqual(result.exception.code, 1)
                        self.assertIn('sudo tail -n 80 /var/log/synthetic-install.log', output.getvalue())
                        self.assertIn('No files or volumes were automatically deleted', output.getvalue())
                    self.assertTrue(log.closed)

    def test_service_prefix_uses_service_account_runtime(self):
        with patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=995)) as lookup:
            prefix = installer.service_prefix()
        lookup.assert_called_once_with('praxis-svc')
        self.assertEqual(prefix, ['sudo', '-u', 'praxis-svc', 'env',
                                 'XDG_RUNTIME_DIR=/run/user/995',
                                 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/995/bus'])

    def test_provider_loader_uses_installation_input_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(installer, 'INPUTS', Path(tmp)):
            helper = installer.provider_module()
            self.assertEqual(helper.STATE, Path(tmp))
            self.assertTrue(callable(helper.save_provider))

    def test_cli_failure_is_nonzero_and_does_not_claim_installation(self):
        # A real CLI child exercises the entry-point exception handler without
        # host writes: preflight or the absent database file stops installation
        # before package installation and service preparation.
        result = subprocess.run([installer.sys.executable, str(ROOT/'scripts/pricetag/install.py'),
                                 '--hostname', 'gateway.example', '--mode', 'mock', '--yes',
                                 '--maas-database-url-file', '/nonexistent-enmaas-test/maas',
                                 '--metering-database-url-file', '/nonexistent-enmaas-test/metering'],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Installation stopped during:', result.stderr)
        self.assertIn('No files or volumes were automatically deleted', result.stderr)
        self.assertNotIn('Installation complete', result.stdout)


class ImageImport(unittest.TestCase):
    def test_source_resolution_and_published_architecture_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(installer, 'CACHE', Path(tmp)), patch.object(installer, 'checkout'), contextlib.redirect_stdout(io.StringIO()):
            args = installer.parse_args(['--hostname', 'gateway.example', '--mode', 'mock', '--plan', '--image-mode', 'source'])
            with patch.object(installer, 'run', return_value='not-a-commit'):
                with self.assertRaisesRegex(ValueError, 'full commit'):
                    installer.acquire_images(args, 'amd64')
            args = installer.parse_args(['--hostname', 'gateway.example', '--mode', 'mock', '--plan', '--image-mode', 'published'])
            def response(command, capture=False, **kwargs):
                if command[-1] == '{{.Architecture}}':
                    return 'arm64'
                if capture:
                    return installer.PRAXIS_REVISION
                return ''
            with patch.object(installer, 'run', side_effect=response):
                with self.assertRaisesRegex(ValueError, 'architecture'):
                    installer.acquire_images(args, 'amd64')
    def test_invalid_source_id_never_starts_export(self):
        with patch.object(installer, 'run', return_value='mutable-tag'), patch.object(installer.subprocess, 'Popen') as process, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'source image ID is invalid'):
                installer.import_image('image', ['service'])
            process.assert_not_called()

    def test_image_id_normalization_and_load_failure(self):
        image_id = 'a'*64
        for returned, expected_error in ((image_id, None), ('sha256:'+image_id, None), ('latest', 'immutable image ID')):
            with self.subTest(returned=returned):
                saved = Mock(stdout=io.BytesIO(b'fake-image'))
                saved.wait.return_value = 0
                saved.poll.return_value = 0
                with patch.object(installer, 'run', side_effect=[image_id, returned]), patch.object(installer.subprocess, 'Popen', return_value=saved), patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), contextlib.redirect_stdout(io.StringIO()):
                    if expected_error:
                        with self.assertRaisesRegex(RuntimeError, expected_error):
                            installer.import_image('image', ['service'])
                    else:
                        self.assertEqual(installer.import_image('image', ['service']), 'sha256:'+image_id)
                    saved.terminate.assert_not_called()
        saved = Mock(stdout=io.BytesIO(b'fake-image'))
        saved.wait.return_value = 0
        saved.poll.return_value = 0
        with patch.object(installer, 'run', return_value=image_id), patch.object(installer.subprocess, 'Popen', return_value=saved), patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=125)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'export/import failed'):
                installer.import_image('image', ['service'])

    def test_loader_exception_terminates_live_exporter(self):
        saved = Mock(stdout=io.BytesIO(b'fake-image'))
        saved.poll.return_value = None
        with patch.object(installer, 'run', return_value='a'*64), patch.object(installer.subprocess, 'Popen', return_value=saved), patch.object(installer.subprocess, 'run', side_effect=OSError('loader failed')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(OSError, 'loader failed'):
                installer.import_image('image', ['service'])
            saved.terminate.assert_called_once()
            saved.wait.assert_called_once()
            self.assertTrue(saved.stdout.closed)


class HostPreflight(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.release = self.home/'os-release'
        self.release.write_text('ID="rhel"\n')
        self.stack.enter_context(patch.object(installer, 'Path', side_effect=lambda p: self.release if p == '/etc/os-release' else Path(p)))
        self.stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))
        for name in ('DEPLOY', 'ADMIN', 'INPUTS', 'CACHE'):
            self.stack.enter_context(patch.object(installer, name, self.home/name))

    def test_fresh_host_and_unprivileged_or_wrong_distribution(self):
        installer.preflight('real')
        installer.preflight('mock')
        with patch.object(installer.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(ValueError, 'sudo'):
                installer.preflight('real')
        self.release.write_text('ID=fedora\n')
        with self.assertRaisesRegex(ValueError, 'RHEL only'):
            installer.preflight('real')

    def test_retained_directories_and_dangling_links_are_not_overwritten(self):
        for name in ('DEPLOY', 'ADMIN', 'INPUTS'):
            path = getattr(installer, name)
            for link in (False, True):
                with self.subTest(name=name, symlink=link):
                    if link:
                        path.symlink_to(self.home/'missing')
                    else:
                        path.mkdir()
                    with self.assertRaisesRegex(ValueError, 'already exists|existing provider inputs'):
                        installer.preflight('real')
                    if link:
                        path.unlink()
                    else:
                        path.rmdir()

    def test_mock_checkout_required_and_cache_ownership_modes(self):
        with patch.object(installer, 'ROOT', self.home):
            with self.assertRaisesRegex(ValueError, 'incomplete checkout'):
                installer.preflight('mock')
        cache = installer.CACHE
        cache.symlink_to(self.home/'missing')
        with self.assertRaisesRegex(ValueError, 'source cache'):
            installer.preflight('real')
        cache.unlink()
        cache.mkdir()
        real_stat = Path.stat
        for uid, mode, allowed in ((0, 0o40755, True), (1000, 0o40755, False),
                                   (0, 0o40775, False), (0, 0o40757, False)):
            with self.subTest(uid=uid, mode=mode):
                def metadata(path, *args, **kwargs):
                    if path == cache:
                        return SimpleNamespace(st_uid=uid, st_mode=mode)
                    return real_stat(path, *args, **kwargs)
                with patch.object(Path, 'stat', metadata):
                    if allowed:
                        installer.preflight('real')
                    else:
                        with self.assertRaisesRegex(ValueError, 'source cache'):
                            installer.preflight('real')


class InstallerLock(unittest.TestCase):
    def test_plan_never_opens_host_lock(self):
        args = SimpleNamespace(plan=True)
        with patch.object(installer, 'parse_args', return_value=args), patch.object(installer, 'install') as install, patch.object(installer.os, 'open') as opened:
            installer.main()
            install.assert_called_once_with(args)
            opened.assert_not_called()

    def test_lock_excludes_second_installer_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp)/'installer.lock'
            real_open = installer.os.open
            def open_lock(path, flags, mode):
                self.assertEqual(path, '/run/pricetag-installer.lock')
                self.assertTrue(flags & installer.os.O_NOFOLLOW)
                self.assertEqual(mode, 0o600)
                return real_open(lock_path, flags, mode)
            args = SimpleNamespace(plan=False, mode='mock')
            with patch.object(installer, 'parse_args', return_value=args), patch.object(installer, 'preflight'), patch.object(installer.os, 'umask'), patch.object(installer.os, 'open', side_effect=open_lock), patch.object(installer, 'install') as install:
                with lock_path.open('w') as held:
                    installer.fcntl.flock(held, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
                    with self.assertRaisesRegex(ValueError, 'Another installer'):
                        installer.main()
                    install.assert_not_called()
                install.side_effect = ValueError('stage failed')
                with self.assertRaisesRegex(ValueError, 'stage failed'):
                    installer.main()
                install.side_effect = None
                installer.main()
                self.assertEqual(install.call_count, 2)


if __name__ == '__main__':
    unittest.main()
