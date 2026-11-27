#!/usr/bin/env python3
"""Fresh-install and benchmark safety boundaries; no host mutations."""
import importlib.machinery
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    loader = importlib.machinery.SourceFileLoader(name.replace('-', '_'), str(ROOT / 'scripts/pricetag' / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


installer = module('install.py')
benchmark = module('benchmark')


class InstallTest(unittest.TestCase):
    def test_explicit_headless_does_not_prompt_on_a_tty(self):
        for mode, provider_flags in (
            ('mock', []),
            ('real', ['--provider', 'openai', '--openai-key-file', '/root/provider.key']),
            ('real', ['--provider', 'external', '--external-api', 'both',
                      '--external-key-file', '/root/provider.key',
                      '--external-openai-url', 'https://provider.example/v1',
                      '--external-messages-url', 'https://provider.example']),
        ):
            with self.subTest(mode=mode, provider=provider_flags), \
                    patch.object(installer.sys.stdin, 'isatty', return_value=True), \
                    patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
                args = installer.parse_args(['--non-interactive', '--yes', '--mode', mode,
                    '--hostname', 'gateway.example', '--no-sample-users', *provider_flags])
                self.assertTrue(args.non_interactive)
                self.assertFalse(args.sample_users)
                self.assertEqual(args.image_mode, 'published')

    def test_explicit_headless_rejects_incomplete_inputs_without_prompting(self):
        base = ['--non-interactive', '--hostname', 'gateway.example']
        cases = [
            ['--mode', 'mock'],  # final confirmation cannot be prompted
            ['--yes'],  # provider missing
            ['--yes', '--provider', 'openai'],  # credential file missing
            ['--yes', '--provider', 'external', '--external-key-file', '/root/provider.key'],
            ['--yes', '--provider', 'external', '--external-api', 'both',
             '--external-key-file', '/root/provider.key', '--external-openai-url', 'https://provider.example/v1'],
        ]
        with patch.object(installer.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=AssertionError('unexpected prompt')), \
                contextlib.redirect_stderr(io.StringIO()):
            for extra in cases:
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    installer.parse_args(base + extra)
            with self.assertRaises(SystemExit):
                installer.parse_args(['--non-interactive', '--yes', '--mode', 'mock'])

    def test_headless_plan_needs_no_confirmation_or_credential_access(self):
        with patch.object(installer.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=AssertionError('unexpected prompt')), \
                patch.object(installer, 'preflight', side_effect=AssertionError('host access')), \
                patch.object(installer, 'provider_module', side_effect=AssertionError('credentials access')), \
                contextlib.redirect_stdout(io.StringIO()):
            args = installer.parse_args(['--non-interactive', '--plan', '--hostname',
                                         'gateway.example', '--provider', 'openai'])
            installer.install(args)
            self.assertTrue(args.plan)
            self.assertFalse(args.yes)

    def test_bootstrap_failure_stops_before_marker_and_explains_recovery(self):
        source = (ROOT / 'scripts/pricetag/start').read_text()
        start = source.index('  if ! python3 "${SCRIPT_DIR}/metering_admin.py"')
        end = source.index('\nfi', start)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bootstrap.json').write_text('{}')
            script = root / 'check.sh'
            script.write_text('set -eu\nDEPLOY_DIR="$1"\nSCRIPT_DIR="$1"\n'
                              'python3() { return 1; }\n'
                              'note() { echo "$*"; }\n'
                              'die() { echo "$*"; exit 1; }\n' + source[start:end])
            result = subprocess.run(['bash', str(script), directory], text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('sudo scripts/pricetag/start', result.stdout)
            self.assertFalse((root / 'bootstrap.done').exists())

    def test_start_migration_recovery_never_kills_a_running_migration(self):
        # Execute the actual recovery block against a recording podman stub:
        # an active long-running migration is never removed (the bound stops
        # this start, not the container), a finished leftover is cleared, and
        # an inspect failure stops the start loudly. No real containers.
        source = (ROOT / 'scripts/pricetag/start').read_text()
        block = source[source.index('if as_service podman container exists pricetag-migrate'):
                       source.index('network_json=')]
        for mode in ('running', 'exited', 'inspect-fail'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'log'
                script = root / 'check.sh'
                script.write_text(
                    'set -euo pipefail\n'
                    'log="$1"; mode="$2"\n'
                    'as_service() {\n'
                    '  shift\n'
                    '  case "$1 $2" in\n'
                    '    "container exists") return 0 ;;\n'
                    '    "container inspect")\n'
                    '      [[ "${mode}" == inspect-fail ]] && return 1\n'
                    '      [[ "${mode}" == running ]] && echo true || echo false\n'
                    '      return 0 ;;\n'
                    '    rm*) printf \'podman %s\\n\' "$*" >> "${log}"; return 0 ;;\n'
                    '    *) printf \'unexpected podman %s\\n\' "$*" >> "${log}"; return 1 ;;\n'
                    '  esac\n'
                    '}\n'
                    'die() { printf \'die: %s\\n\' "$*" >> "${log}"; exit 1; }\n'
                    'sleep() { :; }\n'
                    + block +
                    'printf \'COMPLETED\\n\' >> "${log}"\n')
                result = subprocess.run(['bash', str(script), str(log), mode],
                                        text=True, capture_output=True)
                calls = log.read_text()
                if mode == 'running':
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('still running after 10 minutes', calls)
                    self.assertIn('was left running', calls)
                    # A second invocation must never kill the first one's
                    # migration: no removal of any kind may target it.
                    self.assertNotIn('podman rm', calls)
                    self.assertNotIn('COMPLETED', calls)
                elif mode == 'exited':
                    self.assertEqual(result.returncode, 0)
                    self.assertIn('podman rm pricetag-migrate\n', calls)
                    # The read-only verify leftover is cleared before the
                    # address selection that follows the block.
                    self.assertIn('podman rm -f pricetag-verify\n', calls)
                    self.assertIn('COMPLETED', calls)
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('could not inspect the leftover', calls)
                    self.assertNotIn('podman rm', calls)
                    self.assertNotIn('COMPLETED', calls)

    def test_summary_shows_all_images_and_provider_choices(self):
        sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
        from prepare import POSTGRES
        for mode in ('real', 'mock'):
            with self.subTest(mode=mode):
                flags = ['--mode', mode, '--hostname', 'gateway.example', '--plan',
                         '--image-mode', 'published', '--metering-image', installer.PUBLISHED_METERING_IMAGE]
                if mode == 'real':
                    flags += ['--provider', 'openai', '--provider', 'external', '--external-api', 'both']
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    installer.install(installer.parse_args(flags))
                text = output.getvalue()
                for image in (POSTGRES, installer.PRAXIS_IMAGE, installer.PUBLISHED_MAAS_IMAGE,
                              installer.PUBLISHED_METERING_IMAGE):
                    self.assertEqual(text.count(image), 1)
                self.assertNotIn('  Mode ', text)
                self.assertIn('Mock providers (benchmark)' if mode == 'mock' else
                              'OpenAI, External (OpenAI-compatible + Anthropic Messages)', text)

    def test_sample_users_can_be_omitted_without_omitting_admin(self):
        args = self.image_args('--no-sample-users')
        self.assertFalse(args.sample_users)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            installer.install(args)
        self.assertIn('admin only', output.getvalue())
        self.assertNotIn('admin, alice, bob', output.getvalue())

    def test_start_respects_sample_user_choice_on_repeated_starts(self):
        # Execute the actual startup provisioning block with a recording Python
        # command; no systemd, database or host configuration is touched.
        version = subprocess.check_output(['bash', '-c', 'printf %s "${BASH_VERSINFO[0]}"'], text=True)
        if int(version) < 4:
            self.skipTest('startup requires Bash 4+, as provided by RHEL')
        source = (ROOT / 'scripts/pricetag/start').read_text()
        block = source[source.index('if [[ ! -f "${DEPLOY_DIR}/sample-users"'):source.index("note 'Praxis")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'check.sh'
            script.write_text('set -euo pipefail\nDEPLOY_DIR="$1"\nadmin_dir="$1"\nSCRIPT_DIR="$1"\n'
                              'python3() { printf "%s\\n" "$*"; }\n' + block)
            for selection, expected in [('no', False), ('yes', True)]:
                (root / 'sample-users').write_text(selection + '\n')
                result = subprocess.run(['bash', str(script), directory], capture_output=True, text=True, check=True)
                self.assertEqual('--subject alice' in result.stdout, expected)
                self.assertEqual('--subject bob' in result.stdout, expected)
            (root / 'alice.key').touch()
            result = subprocess.run(['bash', str(script), directory], capture_output=True, text=True, check=True)
            self.assertNotIn('--subject alice', result.stdout)
            self.assertIn('--subject bob', result.stdout)

    def test_real_is_default_and_mock_requires_explicit_selection(self):
        with patch.object(installer.sys.stdin, 'isatty', return_value=False):
            args = installer.parse_args(['--hostname', 'gateway.example',
                                         '--provider', 'openai', '--plan'])
            self.assertEqual(args.mode, 'real')
            mock = installer.parse_args(['--hostname', 'gateway.example', '--mode', 'mock', '--plan'])
            self.assertEqual(mock.mode, 'mock')
            with self.assertRaises(SystemExit):
                installer.parse_args(['--hostname', 'gateway.example', '--plan'])

    def image_args(self, *extra):
        return installer.parse_args(['--mode', 'mock', '--hostname', 'gateway.example', '--plan', *extra])

    def test_published_defaults_and_optional_source_branches(self):
        args = self.image_args()
        self.assertEqual(args.image_mode, 'published')
        self.assertEqual(args.metering_image, installer.PUBLISHED_METERING_IMAGE)
        self.assertEqual(args.maas_image, installer.PUBLISHED_MAAS_IMAGE)
        self.assertIsNone(args.metering_branch)
        self.assertEqual(installer.METERING_REPO, 'https://github.com/redhat-et/pricetag-metering.git')
        source = self.image_args('--image-mode', 'source')
        self.assertEqual(source.maas_image, installer.PUBLISHED_MAAS_IMAGE)
        self.assertEqual(source.metering_branch, installer.METERING_BRANCH)
        self.assertEqual(args.metering_revision, installer.METERING_COMMIT)
        self.assertEqual(args.maas_revision, installer.MAAS_COMMIT)
        self.assertEqual(source.metering_revision, installer.SOURCE_METERING_COMMIT)
        self.assertEqual(module('build-metering').REVISION, installer.SOURCE_METERING_COMMIT)
        for flags in (['--image-mode', 'source', '--metering-branch', '../bad'],
                      ['--image-mode', 'source', '--metering-branch', 'feature',
                       '--metering-revision', 'a' * 40],
                      # MaaS has no source-build mode anymore.
                      ['--maas-branch', 'feature']):
            with self.assertRaises(SystemExit):
                self.image_args(*flags)

    def test_branch_build_records_resolved_commit(self):
        args = self.image_args('--image-mode', 'source', '--metering-branch', 'feature/test')
        resolved = 'c' * 40
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer, 'CACHE', Path(directory)), patch.object(installer, 'checkout') as checkout:
            def execute(command, capture=False, **kwargs):
                if command[0] == 'git':
                    return resolved
                if capture:
                    if command[-1] == '{{.Architecture}}':
                        return 'amd64'
                    if command[-1] == '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}':
                        return '/srv/enmaas-api|[]'
                    return {installer.PRAXIS_IMAGE: installer.PRAXIS_REVISION,
                            args.maas_image: args.maas_revision,
                            installer.METERING_IMAGE: resolved}[command[3]]
            with patch.object(installer, 'run', side_effect=execute) as run:
                installer.acquire_images(args, 'amd64')
            self.assertEqual(len(checkout.call_args_list), 1)
            self.assertEqual(checkout.call_args_list[0].args[2], 'refs/heads/feature/test')
            self.assertEqual(args.metering_revision, resolved)
            self.assertTrue(any('--revision' in c.args[0] and resolved in c.args[0]
                                for c in run.call_args_list))

    def test_demo_fixture_is_mock_only(self):
        self.assertTrue(self.image_args('--demo-fixture').demo_fixture)
        with self.assertRaises(SystemExit):
            installer.parse_args(['--mode', 'real', '--provider', 'openai', '--hostname',
                                  'gateway.example', '--plan', '--demo-fixture'])

    def test_checkout_uses_fetched_head_not_stale_local_branch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, 'run') as run:
            installer.checkout('https://example.invalid/repo', Path(directory) / 'new', 'refs/heads/feature')
            self.assertEqual(run.call_args.args[0][-3:], ['checkout', '--detach', 'FETCH_HEAD'])

    def test_published_images_require_digests_and_skip_source_builds(self):
        for reference in ('quay.io/example/maas:latest', 'sha256:' + 'a' * 64):
            with self.assertRaises(SystemExit):
                self.image_args('--image-mode', 'published', '--maas-image', reference)
        args = self.image_args('--image-mode', 'published',
                               '--maas-image', 'quay.io/example/maas@sha256:' + 'a' * 64,
                               '--metering-image', 'quay.io/example/metering@sha256:' + 'b' * 64)
        def execute(command, capture=False, **kwargs):
            self.assertEqual(command[0], 'podman')
            if capture:
                if command[-1] == '{{.Architecture}}':
                    return 'amd64'
                if command[-1] == '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}':
                    return '/srv/enmaas-api|[]'
                return {installer.PRAXIS_IMAGE: installer.PRAXIS_REVISION,
                        args.maas_image: args.maas_revision,
                        args.metering_image: args.metering_revision}[command[3]]
        with patch.object(installer, 'run', side_effect=execute) as runner, \
                patch.object(installer, 'checkout', side_effect=AssertionError('source checkout')):
            self.assertEqual(installer.acquire_images(args, 'amd64'), (args.maas_image, args.metering_image))
            pulls = [c.kwargs['label'] for c in runner.call_args_list if c.args[0][:2] == ['podman', 'pull']]
            self.assertEqual(pulls, ['Pulling MaaS', 'Pulling PriceTag metering', 'Pulling Praxis'])
        with patch.object(installer, 'run', return_value='wrong-source'):
            with self.assertRaisesRegex(ValueError, 'source revision'):
                installer.acquire_images(args, 'amd64')

    def test_maas_image_layout_is_verified_at_acquire_time(self):
        args = self.image_args('--image-mode', 'published',
                               '--maas-image', 'quay.io/example/maas@sha256:' + 'a' * 64,
                               '--metering-image', 'quay.io/example/metering@sha256:' + 'b' * 64)

        def execute(command, capture=False, **kwargs):
            if capture:
                if command[-1] == '{{.Architecture}}':
                    return 'amd64'
                if command[-1] == '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}':
                    return layout
                return {installer.PRAXIS_IMAGE: installer.PRAXIS_REVISION,
                        args.maas_image: args.maas_revision,
                        args.metering_image: args.metering_revision}[command[3]]
        # start's `python -m app.migrate --workdir /srv/enmaas-api` contract
        # — a CMD-based image with no entrypoint — must be caught at acquire
        # time, before deployment.
        for layout in ('/elsewhere|[]', '/srv/enmaas-api|[./wrapper.sh]'):
            with self.subTest(layout=layout), patch.object(installer, 'run', side_effect=execute):
                with self.assertRaisesRegex(ValueError, 'migration layout'):
                    installer.acquire_images(args, 'amd64')

    def test_praxis_override_requires_digest_and_matching_revision_option(self):
        image = 'quay.io/example/praxis@sha256:' + 'c' * 64
        default = self.image_args()
        self.assertEqual((default.praxis_image, default.praxis_revision),
                         (installer.PRAXIS_IMAGE, installer.PRAXIS_REVISION))
        for flags in (['--praxis-image', image], ['--praxis-revision', 'd' * 40],
                      ['--praxis-image', 'quay.io/example/praxis:latest', '--praxis-revision', 'd' * 40],
                      ['--praxis-image', image, '--praxis-revision', 'main'],
                      ['--praxis-image', 'https://user:secret@example.test/praxis@sha256:' + 'c' * 64,
                       '--praxis-revision', 'd' * 40]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.image_args(*flags)
        for mode in ('source', 'published'):
            args = self.image_args('--image-mode', mode, '--praxis-image', image,
                                   '--praxis-revision', 'd' * 40)
            self.assertEqual((args.praxis_image, args.praxis_revision), (image, 'd' * 40))

    def test_praxis_override_is_pulled_and_provenance_checked(self):
        image = 'quay.io/example/praxis@sha256:' + 'c' * 64
        args = self.image_args('--praxis-image', image, '--praxis-revision', 'd' * 40)
        revisions = {image: args.praxis_revision, args.maas_image: args.maas_revision,
                     args.metering_image: args.metering_revision}
        def execute(command, capture=False, **kwargs):
            if capture:
                if command[-1] == '{{.Architecture}}':
                    return 'amd64'
                if command[-1] == '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}':
                    return '/srv/enmaas-api|[]'
                return revisions[command[3]]
        with patch.object(installer, 'run', side_effect=execute) as runner:
            installer.acquire_images(args, 'amd64')
        self.assertTrue(any(c.args[0] == ['podman', 'pull', image] for c in runner.call_args_list))
        self.assertFalse(any(installer.PRAXIS_IMAGE in c.args[0] for c in runner.call_args_list))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            installer.install(args)
        self.assertIn(image, output.getvalue())
        revisions[image] = 'e' * 40
        with patch.object(installer, 'run', side_effect=execute), self.assertRaisesRegex(ValueError, 'source revision'):
            installer.acquire_images(args, 'amd64')

    def test_source_revision_overrides_reach_checkout_and_build(self):
        args = self.image_args('--image-mode', 'source', '--maas-revision', 'a' * 40,
                               '--metering-revision', 'b' * 40)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer, 'CACHE', Path(directory)), \
                patch.object(installer, 'checkout') as checkout:
            def execute(command, capture=False, **kwargs):
                if capture:
                    if command[-1] == '{{.Architecture}}':
                        return 'amd64'
                    if command[-1] == '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}':
                        return '/srv/enmaas-api|[]'
                    return {installer.PRAXIS_IMAGE: installer.PRAXIS_REVISION,
                            args.maas_image: args.maas_revision,
                            installer.METERING_IMAGE: args.metering_revision}[command[3]]
            with patch.object(installer, 'run', side_effect=execute) as run:
                installer.acquire_images(args, 'amd64')
            # Only metering is built from source; the MaaS revision merely
            # verifies the published image label.
            self.assertEqual([call.args[2] for call in checkout.call_args_list], ['b' * 40])
            commands = [list(map(str, call.args[0])) for call in run.call_args_list]
            self.assertTrue(any('--revision' in c and 'b' * 40 in c for c in commands))
            self.assertTrue(any(c[:2] == ['podman', 'pull'] and args.maas_image in c
                                for c in commands))

    def test_source_build_tags_the_metering_image_that_is_imported(self):
        args = self.image_args('--image-mode', 'source', '--maas-revision', 'a' * 40,
                               '--metering-revision', 'b' * 40)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer, 'CACHE', Path(directory)), \
                patch.object(installer, 'checkout'):
            def execute(command, capture=False, **kwargs):
                if capture:
                    if command[-1] == '{{.Architecture}}':
                        return 'amd64'
                    if command[-1] == '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}':
                        return '/srv/enmaas-api|[]'
                    return {installer.PRAXIS_IMAGE: installer.PRAXIS_REVISION,
                            args.maas_image: args.maas_revision,
                            installer.METERING_IMAGE: args.metering_revision}[command[3]]
            with patch.object(installer, 'run', side_effect=execute) as run:
                images = installer.acquire_images(args, 'amd64')
            commands = [list(map(str, call.args[0])) for call in run.call_args_list]
            built = next(command for command in commands if command[1].endswith('build-metering'))
            self.assertEqual(built[built.index('--tag') + 1], installer.METERING_IMAGE)
            self.assertEqual(images, (args.maas_image, installer.METERING_IMAGE))
            # A standalone build without --tag must still land on the imported
            # tag, and a --revision override derives its own matching tag.
            builder = module('build-metering')
            self.assertEqual(builder.default_tag(builder.REVISION), installer.METERING_IMAGE)
            self.assertEqual(builder.default_tag('b' * 40),
                             'localhost/pricetag-metering:standalone-' + 'b' * 7)

    def test_selected_praxis_is_imported_and_prepared_by_verified_id(self):
        from types import SimpleNamespace
        image = 'quay.io/example/praxis@sha256:' + 'c' * 64
        args = self.image_args('--praxis-image', image, '--praxis-revision', 'd' * 40)
        args.plan, args.yes = False, True
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for name, value in [('preflight', None), ('provider_module', None),
                                ('collect_providers', []), ('open_log', None),
                                ('service_prefix', ['service-exec']),
                                ('acquire_images', (args.maas_image, args.metering_image))]:
                stack.enter_context(patch.object(installer, name, return_value=value))
            stack.enter_context(patch.object(installer.platform, 'machine', return_value='x86_64'))
            stack.enter_context(patch.object(installer.os, 'umask'))
            stack.enter_context(patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=999999)))
            stack.enter_context(patch.object(Path, 'glob', return_value=[]))
            stack.enter_context(patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=1)))
            imported = stack.enter_context(patch.object(installer, 'import_image',
                side_effect=['sha256:' + letter * 64 for letter in 'abc']))
            runner = stack.enter_context(patch.object(installer, 'run'))
            installer.install(args)
        self.assertEqual(imported.call_args_list[0].args, (image, ['service-exec'], 'Praxis'))
        prepare = next(c.args[0] for c in runner.call_args_list
                       if str(ROOT / 'scripts/pricetag/prepare.py') in list(map(str, c.args[0])))
        self.assertEqual(prepare[prepare.index('--praxis-image') + 1], 'sha256:' + 'a' * 64)

    def test_build_output_is_private_and_capture_still_returns_values(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer, 'LOG', None), patch.object(installer, 'LOG_PATH', None):
            screen = io.StringIO()
            try:
                with contextlib.redirect_stdout(screen):
                    installer.open_log(directory)
                    installer.run([sys.executable, '-c', 'print("build detail")'])
                    value = installer.run([sys.executable, '-c', 'print("image-id")'], capture=True)
                self.assertEqual(value, 'image-id')
                self.assertNotIn('build detail', screen.getvalue())
                self.assertIn('build detail', installer.LOG_PATH.read_text())
                self.assertEqual(installer.LOG_PATH.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.run([sys.executable, '-c', 'raise SystemExit(7)'])
            finally:
                installer.LOG.close()

    def test_long_command_reports_progress_without_losing_exit_status(self):
        from unittest.mock import Mock
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('build', 15), 7]
        screen = io.StringIO()
        with contextlib.redirect_stdout(screen):
            self.assertEqual(installer.wait_for(process), 7)
        self.assertIn('Working...', screen.getvalue())

    def test_credentials_fail_before_installation_commands(self):
        args = installer.parse_args(['--mode', 'real', '--hostname', 'gateway.example',
                                     '--provider', 'openai', '--openai-key-file', '/root/key', '--yes'])
        with patch.object(installer, 'preflight'), patch.object(installer, 'provider_module'), \
                patch.object(installer, 'collect_providers', side_effect=ValueError('HTTP 401')), \
                patch.object(installer, 'run', side_effect=AssertionError('installation must not start')):
            with self.assertRaisesRegex(ValueError, 'HTTP 401'):
                installer.install(args)

    def test_cancel_after_discovery_does_not_start_installation(self):
        args = installer.parse_args(['--mode', 'real', '--hostname', 'gateway.example',
                                     '--provider', 'openai', '--plan'])
        args.plan = False
        with patch.object(installer, 'preflight'), patch.object(installer, 'provider_module'), \
                patch.object(installer, 'collect_providers', return_value=[]), \
                patch('builtins.input', return_value='cancel'), \
                patch.object(installer, 'run', side_effect=AssertionError('installation must not start')):
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                installer.install(args)

    def test_interactive_selection_accepts_menu_numbers(self):
        with patch.object(installer.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=['2', '2', '2', '2']):
            args = installer.parse_args(['--hostname', 'gateway.example'])
        self.assertEqual(args.provider, ['external'])
        self.assertEqual(args.external_api, 'anthropic')
        self.assertFalse(args.sample_users)

    def test_external_selection_requires_one_provider_and_records_api(self):
        with patch.object(installer.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(SystemExit):
                installer.parse_args(['--mode', 'real', '--hostname', 'gateway.example', '--yes'])
            args = installer.parse_args(['--mode', 'real', '--hostname', 'gateway.example',
                '--provider', 'external', '--external-api', 'anthropic', '--external-key-file', '/root/key',
                '--external-messages-url', 'provider.example', '--yes'])
            self.assertEqual(args.external_api, 'anthropic')
            self.assertEqual(args.provider, ['external'])

    def test_external_database_files_must_be_supplied_together_and_absolute(self):
        base = ['--mode', 'mock', '--hostname', 'gateway.example', '--plan']
        with patch.object(installer.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(SystemExit):
                installer.parse_args(base + ['--maas-database-url-file', '/root/maas.url'])
            with self.assertRaises(SystemExit):
                installer.parse_args(base + ['--maas-database-url-file', 'maas.url',
                                             '--metering-database-url-file', '/root/metering.url'])
            with self.assertRaises(SystemExit):
                installer.parse_args(base + ['--external-database-ca-file', '/root/ca.crt'])
            args = installer.parse_args(base + ['--maas-database-url-file', '/root/maas.url',
                                                '--metering-database-url-file', '/root/metering.url'])
            self.assertEqual(args.maas_database_url_file, Path('/root/maas.url'))
            self.assertEqual(args.metering_database_url_file, Path('/root/metering.url'))

    def test_external_database_plan_does_not_read_credentials_or_show_local_database(self):
        args = installer.parse_args(['--mode', 'mock', '--hostname', 'gateway.example', '--plan',
                                     '--maas-database-url-file', '/root/does-not-exist.url',
                                     '--metering-database-url-file', '/root/does-not-exist.url'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            installer.install(args)
        text = output.getvalue()
        self.assertIn('PostgreSQL external (pre-provisioned)', text)
        self.assertNotIn('does-not-exist.url', text)
        self.assertNotIn('docker.io/library/postgres@', text)

    def test_plan_runs_without_privileges_and_does_not_invoke_commands(self):
        with patch.object(sys, 'argv', ['install', '--mode', 'mock', '--hostname', 'gateway.example', '--plan']), \
                patch.object(installer, 'run', side_effect=AssertionError('mutation')), \
                patch.object(installer, 'preflight', side_effect=AssertionError('host inspection')):
            installer.main()

    def test_unattended_real_install_requires_credentials_and_endpoint(self):
        base = ['--mode', 'real', '--hostname', 'gateway.example', '--provider', 'pricetag', '--yes']
        with patch.object(installer.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(SystemExit):
                installer.parse_args(base)
            with self.assertRaises(SystemExit):
                installer.parse_args(base + ['--pricetag-key-file', '/root/key'])
            args = installer.parse_args(base + ['--pricetag-key-file', '/root/key',
                                               '--pricetag-messages-url', 'provider.example'])
            self.assertEqual(args.pricetag_key_file, Path('/root/key'))

    def test_existing_configuration_refused_without_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(installer.os, 'geteuid', return_value=0), \
                    patch.object(installer.Path, 'read_text', return_value='ID=rhel\n'), \
                    patch.object(installer, 'DEPLOY', Path(directory)), \
                    patch.object(installer, 'run', side_effect=AssertionError('mutation')):
                with self.assertRaisesRegex(ValueError, 'already exists'):
                    installer.preflight('real')

    def test_rejects_url_or_shell_input_as_hostname(self):
        for value in ('https://gateway.example', 'gateway.example:8443', 'a\nb', '-x', 'a/b', 'a;id', ''):
            self.assertFalse(installer.valid_hostname(value), value)
        for value in ('gateway.example', '127.0.0.1'):
            self.assertTrue(installer.valid_hostname(value))

    def test_checkout_commands_use_readable_source_permissions(self):
        from types import SimpleNamespace
        with patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(stdout='')) as execute:
            installer.run(['git', 'clone', 'https://example.invalid/source', '/tmp/source'])
            self.assertEqual(execute.call_args.kwargs['umask'], 0o022)
            installer.run(['true'])
            self.assertEqual(execute.call_args.kwargs['umask'], -1)

    def test_import_looks_up_loaded_image_by_id_not_registry_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / 'podman'
            engine.write_text('#!/bin/sh\nexit 0\n')
            engine.chmod(0o755)
            import os
            for image_id in ('a' * 64, 'sha256:' + 'a' * 64):
                with patch.dict(os.environ, {'PATH': directory + ':' + os.environ['PATH']}), \
                        patch.object(installer, 'run', side_effect=[image_id, image_id]) as execute, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(installer.import_image('registry/image@sha256:manifest', ['true'], 'MaaS'),
                                     'sha256:' + 'a' * 64)
                    self.assertIn(image_id, execute.call_args.args[0])
                    self.assertIn('Importing MaaS into praxis-svc', output.getvalue())
                    self.assertIn('registry/image@sha256:manifest', output.getvalue())
                    self.assertIn('OK: MaaS imported', output.getvalue())

    def test_mock_rejects_provider_flags(self):
        with self.assertRaises(SystemExit):
            installer.parse_args(['--mode', 'mock', '--hostname', 'localhost', '--provider', 'openai'])

    def test_image_export_failure_does_not_prepare_or_inspect(self):
        # Real pipe behavior: exporter failure must survive an importer that exits 0.
        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / 'podman'
            engine.write_text('#!/bin/sh\nexit 7\n')
            engine.chmod(0o755)
            import os
            with patch.dict(os.environ, {'PATH': directory + ':' + os.environ['PATH']}), \
                    patch.object(installer, 'run', side_effect=['a' * 64]):
                with self.assertRaises(RuntimeError):
                    installer.import_image('unused', ['true'])

    def test_benchmark_rejects_real_models_and_provider_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'models.json').write_text('[{"id":"openai/real","model":"real"}]')
            with self.assertRaises(ValueError):
                benchmark.require_mock(path)
            (path / 'models.json').write_text('[{"id":"fixture","model":"fixture"}]')
            (path / 'quadlets').mkdir()
            (path / 'quadlets/pricetag-gateway.container').write_text(
                'Network=pricetag-mock.network\nSecret=real,type=env,target=KEY\n')
            with self.assertRaises(ValueError):
                benchmark.require_mock(path)

    def test_mock_guard_accepts_rendered_fixture_and_rejects_remote_endpoints(self):
        sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
        import prepare
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config, models = prepare.mock_inputs()
            (path / 'models.json').write_text(json.dumps(models))
            (path / 'quadlets').mkdir()
            (path / 'quadlets/pricetag-gateway.container').write_text(
                prepare.gateway_quadlet_body('sha256:' + 'a' * 64, [], '', True))
            (path / 'gateway').mkdir()
            rendered = prepare.render(config, models)
            (path / 'gateway/gateway.json').write_text(json.dumps(rendered))
            benchmark.require_mock(path)
            for chain in rendered['filter_chains']:
                for item in chain['filters']:
                    if item.get('filter') == 'load_balancer':
                        item['clusters'][0]['endpoints'] = ['remote.example:443']
            (path / 'gateway/gateway.json').write_text(json.dumps(rendered))
            with self.assertRaises(ValueError):
                benchmark.require_mock(path)

    def test_mock_guard_accepts_managed_mock_aliases_only(self):
        sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
        import demo_config
        import gateway
        document = {'providers': [{'name': 'mock', 'urls': {
            'openai': 'http://pricetag-provider:18080/v1',
            'anthropic': 'http://pricetag-provider:18081'}, 'messages_auth': 'x-api-key'}],
            'models': [{'id': 'mock/cheap', 'model': 'fixture-cheap', 'provider': 'mock',
                        'apis': ['openai', 'anthropic'], 'context': 32768, 'output': 4096}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'quadlets').mkdir(); (path / 'gateway').mkdir()
            (path / 'models.json').write_text(json.dumps(document['models']))
            rendered = gateway.render(demo_config.native_inputs(document), document['models'])
            (path / 'gateway/gateway.json').write_text(json.dumps(rendered))
            unit = path / 'quadlets/pricetag-gateway.container'
            unit.write_text('Network=pricetag-mock.network\nSecret=praxis-mock-api-key-v2,type=env,target=CUSTOM_MOCK_API_KEY\n')
            benchmark.require_mock(path)
            unit.write_text(unit.read_text().replace('praxis-mock-api-key-v2', 'real-provider-key'))
            with self.assertRaises(ValueError):
                benchmark.require_mock(path)

    def test_sampler_errors_and_disk_pressure_fail(self):
        with self.assertRaises(ValueError):
            benchmark.check_sample({'postgres': {'error': 'failed'}})
        with self.assertRaises(ValueError):
            benchmark.check_sample({'disk_available_percent': 29})

    def test_budget_only_rejects_key_rotation(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/pricetag/user'),
                                 '--subject', 'alice', '--no-key', '--rotate'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--no-key cannot', result.stderr)


if __name__ == '__main__':
    unittest.main()
