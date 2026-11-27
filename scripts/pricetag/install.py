#!/usr/bin/env python3
"""Install the pinned gateway, MaaS and PriceTag on a fresh RHEL host."""
import argparse
import fcntl
import importlib.machinery
import importlib.util
import ipaddress
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
# MaaS is always the published enmaas-api image (built from the private
# redhat-et/enmaas-api source); its revision is only verified against the
# image label here, never built from source.
MAAS_COMMIT = 'aea9c56749117e78dc1a715f7d548b2d569b24d5'
# Published image provenance remains separate from the source-build candidate.
METERING_COMMIT = 'ce9c6c51b8dff178637003945b517255c54fc853'
# The metering source line is the standalone-management branch of
# redhat-et/pricetag-metering; PR #149 merged, and main also carries the
# pinned revision. The branch remains the reviewed integration line.
SOURCE_METERING_COMMIT = 'e4ba826d5b1469d7f1167428792e498ffb4d92c7'
METERING_REPO = 'https://github.com/redhat-et/pricetag-metering.git'
METERING_BRANCH = 'standalone-management'
PRAXIS_IMAGE = 'quay.io/redhat-et/praxis-ai@sha256:240bcdaa73f4ca81a670dc0451db3072aa5190eca71f2af13c5ddfb75e49abdc'
PRAXIS_REVISION = '548f61ccef002f275056982d5104dc161e886210'
# Derive the local source-build tag from the pin so a re-pin cannot leave a
# stale label behind (build-metering derives its standalone default the same way).
METERING_IMAGE = 'localhost/pricetag-metering:standalone-' + SOURCE_METERING_COMMIT[:7]
PUBLISHED_MAAS_IMAGE = 'quay.io/redhat-et/enmaas-api@sha256:fb8e02ab32223ca91507b112e8aa21b5281445100c7ce2023d06065c70a3ac82'
PUBLISHED_METERING_IMAGE = 'quay.io/redhat-et/secure-single-server-pricetag-metering@sha256:dabb2f35f54c30c2c387b3bf298b66f3b84a4110ddf3a78a204c7d59c5de2f1a'
CACHE = Path('/var/cache/pricetag-installer')
DEPLOY = Path('/etc/praxis-pricetag')
ADMIN = Path('/root/pricetag-admin')
INPUTS = Path('/root/pricetag-provider-inputs')
LOG = None
LOG_PATH = None
STAGE = 'Setup'


def stage(number, title):
    global STAGE
    STAGE = title
    print(f'\n[{number}/6] {title}\n' + '-' * 54, flush=True)


def open_log(directory='/var/log'):
    global LOG, LOG_PATH
    fd, name = tempfile.mkstemp(prefix='praxis-install-', suffix='.log', dir=directory)
    LOG_PATH = Path(name)
    LOG = os.fdopen(fd, 'w', buffering=1)
    print(f'\nDetailed log: {LOG_PATH} (private)\n'
          f'Follow in another SSH session: sudo tail -f {LOG_PATH}', flush=True)


def wait_for(process):
    started = time.monotonic()
    while True:
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print(f'  Working... {int(time.monotonic() - started)}s elapsed', flush=True)


def valid_hostname(value):
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return len(value) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                                         for part in value.split('.'))


def run(command, capture=False, label=None):
    command = [str(x) for x in command]
    if LOG is not None:
        LOG.write('+ ' + shlex.join(command) + '\n')
        if not capture:
            print('  ' + (label or ('Running ' + Path(command[0]).name)) + '...', flush=True)
            with subprocess.Popen(command, cwd='/tmp', text=True, stdout=LOG, stderr=LOG,
                                  umask=0o022 if command[0] == 'git' else -1) as process:
                code = wait_for(process)
            if code:
                raise subprocess.CalledProcessError(code, command)
            print('  OK', flush=True)
            return None
    result = subprocess.run(command, check=True, cwd='/tmp', text=True,
                            umask=0o022 if command[0] == 'git' else -1,
                            stderr=LOG,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def service_prefix():
    uid = pwd.getpwnam('praxis-svc').pw_uid
    return ['sudo', '-u', 'praxis-svc', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
            f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus']


def import_image(image, prefix, component="Image"):
    # Stream between stores and check both processes; a failed save must not be
    # hidden by a successful load. Never use the administrator's cwd as praxis-svc.
    print(f'  Importing {component} into praxis-svc...\n    {image}', flush=True)
    source_id = run(['podman', 'image', 'inspect', image, '--format', '{{.Id}}'], True)
    if not re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', source_id):
        raise RuntimeError('source image ID is invalid')
    save = subprocess.Popen(['podman', 'save', image], stdout=subprocess.PIPE, stderr=LOG, cwd='/tmp')
    try:
        with save.stdout:
            loaded = subprocess.run(prefix + ['podman', 'load'], stdin=save.stdout, cwd='/tmp',
                                    stdout=LOG, stderr=LOG)
        saved = save.wait()
        if saved or loaded.returncode:
            raise RuntimeError('image export/import failed; no deployment prepared')
    finally:
        if save.poll() is None:
            save.terminate()
            save.wait()
    image_id = run(prefix + ['podman', 'image', 'inspect', source_id, '--format', '{{.Id}}'], True)
    if re.fullmatch(r'[0-9a-f]{64}', image_id):
        image_id = 'sha256:' + image_id
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise RuntimeError('image import did not return an immutable image ID')
    print(f'  OK: {component} imported', flush=True)
    return image_id


def checkout(repository, directory, revision=None):
    if directory.exists():
        # Never discard local changes or silently repurpose a cache checkout.
        if directory.is_symlink() or not (directory / '.git').is_dir():
            raise RuntimeError('invalid source cache: ' + str(directory))
        if run(['git', '-C', directory, 'remote', 'get-url', 'origin'], True) != repository:
            raise RuntimeError('source cache has an unexpected origin')
        if run(['git', '-C', directory, 'status', '--porcelain'], True):
            raise RuntimeError('source cache is dirty; review it before retrying')
    else:
        run(['git', 'clone', repository, directory])
    if revision:
        run(['git', '-C', directory, 'fetch', 'origin', revision])
        run(['git', '-C', directory, 'checkout', '--detach', 'FETCH_HEAD'])


def preflight(mode):
    if os.geteuid() != 0:
        raise ValueError('run with sudo on the RHEL host')
    if not re.search(r'^ID="?rhel"?$', Path('/etc/os-release').read_text(), re.M):
        raise ValueError('this installer supports RHEL only')
    if mode == 'mock' and not (ROOT / 'tests/common/provider.py').is_file():
        raise ValueError('incomplete checkout: tests/common/provider.py is required for mock mode')
    for path in (DEPLOY, ADMIN):
        if path.exists() or path.is_symlink():
            raise ValueError(f'{path} already exists; this is a fresh installer, not an upgrade command')
    if INPUTS.exists() or INPUTS.is_symlink():
        raise ValueError('existing provider inputs; use the manual setup steps to resume')
    if CACHE.is_symlink() or (CACHE.exists() and (CACHE.stat().st_uid != 0 or CACHE.stat().st_mode & 0o022)):
        raise ValueError('source cache must be root-owned and not group/world writable')


def choice(label, options, default):
    print('\n' + label)
    values = list(options)
    for number, value in enumerate(values, 1):
        print(f'  {number}. {options[value]}' + ('  (default)' if value == default else ''))
    while True:
        value = input(f'Choice [{values.index(default) + 1}]: ').strip().lower() or default
        if value.isdigit() and 1 <= int(value) <= len(values):
            value = values[int(value) - 1]
        if value in options:
            print('Selected: ' + options[value])
            return value
        print('Choose one of: ' + ', '.join(values))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('real', 'mock'), default='real',
                   help='real providers (default); use mock for synthetic benchmarks')
    p.add_argument('--demo-fixture', action='store_true', help='mock mode only: two model aliases for the UI demo')
    p.add_argument('--hostname', help='public IPv4 or DNS name; no scheme, path or port')
    p.add_argument('--provider', action='append', choices=('openai', 'external', 'pricetag'), default=[],
                   help='real mode requires one or more providers; pricetag is the legacy external profile')
    p.add_argument('--openai-key-file', type=Path)
    p.add_argument('--external-key-file', type=Path)
    p.add_argument('--external-api', choices=('openai', 'anthropic', 'both'),
                   help='external endpoint protocol(s): OpenAI-compatible, Anthropic Messages or both')
    p.add_argument('--external-messages-url')
    p.add_argument('--external-openai-url')
    p.add_argument('--external-messages-auth', choices=('x-api-key', 'bearer'), default='x-api-key')
    p.add_argument('--pricetag-key-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--pricetag-messages-url', help=argparse.SUPPRESS)
    p.add_argument('--pricetag-openai-url', help=argparse.SUPPRESS)
    p.add_argument('--monthly-usd', type=int, default=5, help='default per-user monthly USD allowance; default 5')
    p.add_argument('--tls-bundle', type=Path, help='pre-issued server.pem, server.key and ca.pem')
    p.add_argument('--oidc-config', type=Path, help='private JSON file for external OIDC login')
    p.add_argument('--maas-database-url-file', type=Path,
                   help='private URL file for the existing external maas database')
    p.add_argument('--metering-database-url-file', type=Path,
                   help='private URL file for the existing external metering database')
    p.add_argument('--external-database-ca-file', type=Path,
                   help='CA certificate required for sslmode=verify-full')
    p.add_argument('--sample-users', action=argparse.BooleanOptionalAction, default=None,
                   help='create Alice and Bob in addition to the required administrator')
    p.add_argument('--yes', action='store_true', help='skip final confirmation, not missing setup prompts')
    p.add_argument('--non-interactive', action='store_true',
                   help='never prompt; require complete options and protected credential files')
    p.add_argument('--plan', action='store_true', help='print stages without credentials, discovery or host changes')
    p.add_argument('--image-mode', choices=('source', 'published'),
                   help='metering source build or digest-pinned published images; MaaS is always published')
    p.add_argument('--praxis-image', help='Praxis image with @sha256 digest; requires --praxis-revision')
    p.add_argument('--praxis-revision', help='expected full Git SHA in the selected Praxis image label')
    p.add_argument('--maas-image', help='published enmaas-api MaaS image, including @sha256 digest')
    p.add_argument('--metering-image', help='published PriceTag image, including @sha256 digest')
    p.add_argument('--maas-revision',
                   help='full enmaas-api source SHA expected in the published MaaS image label')
    metering_source = p.add_mutually_exclusive_group()
    metering_source.add_argument('--metering-revision', help='full PriceTag source SHA to build or verify')
    metering_source.add_argument('--metering-branch', help='source mode: branch in the PriceTag source repository')
    args = p.parse_args(argv)
    if (args.praxis_image is None) != (args.praxis_revision is None):
        p.error('provide both --praxis-image and --praxis-revision')
    if args.praxis_image is None:
        args.praxis_image, args.praxis_revision = PRAXIS_IMAGE, PRAXIS_REVISION
    if not re.fullmatch(r'[a-z0-9][a-z0-9.:-]*/[a-z0-9_./-]+@sha256:[0-9a-f]{64}', args.praxis_image):
        p.error('--praxis-image must include a registry/repository and sha256 digest')
    if not re.fullmatch(r'[0-9a-f]{40}', args.praxis_revision):
        p.error('--praxis-revision must be a full lowercase Git commit SHA')
    interactive = sys.stdin.isatty() and not args.plan and not args.non_interactive
    if args.non_interactive and not args.plan and not args.yes:
        p.error('--non-interactive requires --yes')
    if not args.hostname and interactive:
        args.hostname = input('Gateway public IPv4 or DNS name: ').strip()
    if not args.hostname or not valid_hostname(args.hostname):
        p.error('provide --hostname with a valid IPv4 or DNS name')
    if not 1 <= args.monthly_usd <= 10000:
        p.error('--monthly-usd must be 1–10000')
    if args.maas_database_url_file is not None or args.metering_database_url_file is not None:
        if args.maas_database_url_file is None or args.metering_database_url_file is None:
            p.error('external databases require both --maas-database-url-file and --metering-database-url-file')
        if not args.maas_database_url_file.is_absolute() or not args.metering_database_url_file.is_absolute():
            p.error('external database URL file paths must be absolute')
        if args.external_database_ca_file is not None and not args.external_database_ca_file.is_absolute():
            p.error('external database CA file path must be absolute')
    elif args.external_database_ca_file is not None:
        p.error('--external-database-ca-file requires both external database URL files')
    if args.mode == 'real' and not args.provider:
        if interactive:
            selected = choice('At least one provider is required:', {
                'openai': 'OpenAI directly',
                'external': 'External API provider',
                'both': 'OpenAI directly + external API provider'}, 'both')
            args.provider = ['openai', 'external'] if selected == 'both' else [selected]
        else:
            p.error('real mode requires --provider openai and/or --provider external')
    if args.demo_fixture and args.mode != 'mock':
        p.error('--demo-fixture requires --mode mock')
    if args.mode == 'mock' and args.provider:
        p.error('mock mode cannot configure real providers')
    if 'external' in args.provider and not args.external_api:
        if interactive:
            args.external_api = choice('External provider APIs:', {
                'openai': 'OpenAI-compatible (Chat/Responses)',
                'anthropic': 'Anthropic Messages', 'both': 'Both APIs'}, 'both')
        elif args.plan:
            args.external_api = 'both'
        else:
            p.error('--provider external requires --external-api openai|anthropic|both')
    if args.external_api == 'openai' and args.external_messages_url:
        p.error('Messages URL requires --external-api anthropic or both')
    if args.external_api == 'anthropic' and args.external_openai_url:
        p.error('OpenAI URL requires --external-api openai or both')
    for provider in ('openai', 'external', 'pricetag'):
        key_file = getattr(args, provider + '_key_file')
        if key_file and not key_file.is_absolute():
            p.error('credential file paths must be absolute')
        if key_file and provider not in args.provider:
            p.error('--' + provider + '-key-file requires that provider')
    for provider in ('external', 'pricetag'):
        if any(getattr(args, provider + '_' + field) for field in ('messages_url', 'openai_url')) and provider not in args.provider:
            p.error(provider + ' URLs require --provider ' + provider)
    if args.external_api and 'external' not in args.provider:
        p.error('--external-api requires --provider external')
    if args.pricetag_key_file and not (args.pricetag_messages_url or args.pricetag_openai_url):
        p.error('--pricetag-key-file requires a PriceTag URL')
    if not args.plan and (args.non_interactive or not sys.stdin.isatty()) and args.mode == 'real':
        if not args.yes:
            p.error('noninteractive installation requires --yes')
        if any(not getattr(args, provider + '_key_file') for provider in args.provider):
            p.error('noninteractive real installation requires a private key file for each provider')
        if 'external' in args.provider:
            required = ['openai', 'messages'] if args.external_api == 'both' else [
                'messages' if args.external_api == 'anthropic' else 'openai']
            if any(not getattr(args, 'external_' + api + '_url') for api in required):
                p.error('supply an endpoint for each selected external API')
    if args.sample_users is None:
        args.sample_users = True
        if interactive and not args.yes:
            args.sample_users = choice('Initial users:', {
                'yes': 'Admin + sample users Alice and Bob',
                'no': 'Admin only; add users later'}, 'yes') == 'yes'
    if not args.image_mode:
        if args.maas_image or args.metering_image:
            args.image_mode = 'published'
        elif interactive and not args.yes:
            args.image_mode = choice('Application images:', {
                'published': 'Use tested published images (recommended)',
                'source': 'Build latest demo branches (compile on RHEL)'}, 'published')
        else:
            args.image_mode = 'published'
    for component in ('maas', 'metering'):
        # MaaS is always the published enmaas-api image; source mode only
        # selects the metering build.
        image = getattr(args, component + '_image')
        if args.image_mode == 'source' and component == 'metering':
            if image:
                p.error('--metering-image requires --image-mode published')
        else:
            if not image:
                image = PUBLISHED_MAAS_IMAGE if component == 'maas' else PUBLISHED_METERING_IMAGE
                setattr(args, component + '_image', image)
            if not re.fullmatch(r'[a-z0-9][a-z0-9.:-]*/[a-z0-9_./-]+@sha256:[0-9a-f]{64}', image):
                p.error('--' + component + '-image must include a registry/repository and sha256 digest')
        revision = getattr(args, component + '_revision')
        branch = getattr(args, 'metering_branch', None) if component == 'metering' else None
        if args.image_mode == 'source' and component == 'metering' and not (revision or branch):
            branch = METERING_BRANCH
            setattr(args, 'metering_branch', branch)
        if branch:
            if args.image_mode != 'source':
                p.error('branch selection requires --image-mode source')
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', branch)
                    or any(part in ('', '.', '..') or part.endswith(('.', '.lock')) for part in branch.split('/'))
                    or '..' in branch):
                p.error('invalid source branch name')
        if not revision:
            expected = MAAS_COMMIT if component == 'maas' else (
                SOURCE_METERING_COMMIT if args.image_mode == 'source' else METERING_COMMIT)
            setattr(args, component + '_revision', expected)
        if not re.fullmatch(r'[0-9a-f]{40}', getattr(args, component + '_revision')):
            p.error('--' + component + '-revision must be a full lowercase Git commit SHA')
    args.provider = list(dict.fromkeys(args.provider))
    return args


def provider_module():
    sys.path.insert(0, str(ROOT / 'scripts/common'))
    loader = importlib.machinery.SourceFileLoader('installer_providers', str(ROOT / 'scripts/pricetag/providers'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.STATE = INPUTS
    return module


def collect_providers(args, helper):
    """Collect and validate every provider before any installation work."""
    collected = []
    for name in args.provider:
        overrides, apis = {}, None
        if name != 'openai':
            overrides = {'openai': getattr(args, name + '_openai_url'),
                         'anthropic': getattr(args, name + '_messages_url')}
        if name == 'external':
            apis = ['openai', 'anthropic'] if args.external_api == 'both' else [args.external_api]
        helper.MESSAGES_AUTH = args.external_messages_auth if name == 'external' else 'x-api-key'
        urls = helper.provider_urls(name, overrides, apis)
        key = helper.collect_credential(name, urls, getattr(args, name + '_key_file'))
        settings = helper.provider_settings(name, 'openai' if name == 'openai' else 'both',
                    next(iter(urls.values())), key, urls=urls, allocate_secret=False)
        collected.append((name, settings, key))
    return collected


def acquire_images(args, arch):
    stage(3, 'Acquire application images')
    # MaaS is always the published enmaas-api image; source mode only builds
    # the metering component from its reviewed source line.
    print('  MaaS image: ' + args.maas_image, flush=True)
    run(['podman', 'pull', args.maas_image], label='Pulling MaaS')
    if args.image_mode == 'source':
        print('  Resolving metering source and building; this can take several minutes.', flush=True)
        CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
        branch = args.metering_branch
        revision = args.metering_revision
        checkout(METERING_REPO, CACHE / 'metering', 'refs/heads/' + branch if branch else revision)
        if branch:
            revision = run(['git', '-C', CACHE / 'metering', 'rev-parse', 'HEAD'], True)
            if not re.fullmatch(r'[0-9a-f]{40}', revision):
                raise ValueError('Source branch did not resolve to a full commit')
            args.metering_revision = revision
        print(f'  metering: {branch or "commit"} -> {revision}', flush=True)
        run([sys.executable, ROOT / 'scripts/pricetag/build-metering', '--source', CACHE / 'metering',
             '--revision', args.metering_revision, '--tag', METERING_IMAGE,
             '--platform', 'linux/' + arch], label='Building PriceTag')
        images = (args.maas_image, METERING_IMAGE)
    else:
        print('  PriceTag metering image: ' + args.metering_image, flush=True)
        run(['podman', 'pull', args.metering_image], label='Pulling PriceTag metering')
        images = (args.maas_image, args.metering_image)
    print('  Praxis image: ' + args.praxis_image, flush=True)
    run(['podman', 'pull', args.praxis_image], label='Pulling Praxis')
    for image, expected in zip((args.praxis_image, *images),
                               (args.praxis_revision, args.maas_revision, args.metering_revision)):
        revision = run(['podman', 'image', 'inspect', image, '--format',
                        '{{index .Labels "org.opencontainers.image.revision"}}'], True)
        if revision != expected:
            raise ValueError('Image source revision does not match expected commit: ' + image)
        actual_arch = run(['podman', 'image', 'inspect', image, '--format', '{{.Architecture}}'], True)
        if actual_arch != arch:
            raise ValueError('Image architecture does not match this host: ' + image)
        if image == args.maas_image:
            # start runs `python -m app.migrate` from this image's working
            # directory before the service starts. Assert that documented
            # contract here — the working directory, the absence of an
            # entrypoint (the migration is a plain command override on a
            # CMD-based image), and that the module imports with no network
            # or database — so a future pin bump to a differently laid-out
            # image fails during installation instead of at every start.
            layout = run(['podman', 'image', 'inspect', image, '--format',
                          '{{.Config.WorkingDir}}|{{.Config.Entrypoint}}'], True)
            workdir, _, entrypoint = layout.partition('|')
            if workdir != '/srv/enmaas-api' or entrypoint not in ('', '[]'):
                raise ValueError('MaaS image lacks the documented CMD-based migration layout '
                                 '(/srv/enmaas-api, no entrypoint): ' + image)
            run(['podman', 'run', '--rm', '--network', 'none', '--workdir', '/srv/enmaas-api',
                 image, 'python', '-c', 'import app.migrate'],
                label='Verifying MaaS migration layout')
    return images


def install(args):
    sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
    from prepare import POSTGRES, validate_external_database_urls
    apis = {'openai': 'OpenAI-compatible', 'anthropic': 'Anthropic Messages',
            'both': 'OpenAI-compatible + Anthropic Messages'}
    providers = {'openai': 'OpenAI', 'external': 'External (' + apis.get(args.external_api, 'both APIs') + ')',
                 'pricetag': 'External (legacy PriceTag profile)'}
    print('\nRemote gateway with PriceTag\n' + '=' * 54)
    print(f'  Gateway    https://{args.hostname}:8443')
    print('  Providers  ' + ('Mock providers (benchmark)' if args.mode == 'mock' else
                            ', '.join(providers[name] for name in args.provider)))
    print(f'  Images     {args.image_mode}')
    print('  Praxis     ' + args.praxis_image)
    print('  PostgreSQL ' + ('external (pre-provisioned)' if args.maas_database_url_file else POSTGRES))
    print('  MaaS       ' + args.maas_image)
    print('  PriceTag   ' + (METERING_IMAGE if args.image_mode == 'source' else args.metering_image))
    if args.image_mode == 'source':
        print('  PriceTag ref ' + (args.metering_branch or args.metering_revision))
    print('  Users      ' + ('admin, alice, bob' if args.sample_users else 'admin only'))
    print(f'  Budget     ${args.monthly_usd}/month per ordinary user')
    print('\n  1. Validate provider setup   4. Import images')
    print('  2. Prepare RHEL             5. Configure gateway')
    print('  3. Build/pull images        6. Start and verify services')
    if args.plan:
        print('Expected MaaS commit: ' + args.maas_revision)
        if not args.metering_branch:
            print('Expected PriceTag commit: ' + args.metering_revision)
        return
    if args.maas_database_url_file is not None:
        try:
            validate_external_database_urls(args.maas_database_url_file.read_text(),
                                            args.metering_database_url_file.read_text(),
                                            args.external_database_ca_file)
        except (OSError, ValueError) as error:
            raise ValueError(str(error)) from None
    if args.oidc_config is not None:
        if args.metering_revision == 'ce9c6c51b8dff178637003945b517255c54fc853':
            raise ValueError('This metering revision has no OIDC handler; select the tested source-build candidate')
        from oidc_config import load as load_oidc
        load_oidc(args.oidc_config, f'https://{args.hostname}:8443')
    if args.tls_bundle is not None:
        from tls_bundle import validate as validate_tls_bundle
        validate_tls_bundle(args.tls_bundle, args.hostname)
    preflight(args.mode)
    helper = provider_module()
    stage(1, 'Validate provider setup')
    prepared = collect_providers(args, helper)
    print('\nSetup validated. Ready to install the configuration above.')
    if not args.yes and input('Install on this host? Type install: ').strip() != 'install':
        raise ValueError('installation cancelled')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if not arch:
        raise ValueError('supported architectures are x86_64 and aarch64')
    os.umask(0o077)
    open_log()
    stage(2, 'Prepare RHEL')
    run(['dnf', 'install', '-y', 'podman', 'git', 'python3', 'python3-pyyaml', 'openssl',
         'curl', 'sudo', 'policycoreutils-python-utils', 'shadow-utils'])
    run([ROOT / 'scripts/common/install', '--prepare'])
    prefix = service_prefix()
    # Refuse retained data or Quadlets even if someone removed the config directory.
    if (args.maas_database_url_file is None and subprocess.run(
            prefix + ['podman', 'volume', 'exists', 'pricetag-data'], cwd='/tmp').returncode != 1):
        raise ValueError('existing data volume or storage error; review the host instead of reinstalling')
    uid = pwd.getpwnam('praxis-svc').pw_uid
    units = Path(f'/etc/containers/systemd/users/{uid}')
    if list(units.glob('pricetag*')):
        raise ValueError('existing PriceTag Quadlets; review the host instead of reinstalling')
    application_images = acquire_images(args, arch)
    stage(4, 'Import images into the service account')
    ids = [import_image(image, prefix, component) for component, image in
           zip(('Praxis', 'MaaS', 'PriceTag metering'), (args.praxis_image, *application_images))]
    stage(5, 'Configure providers, TLS and budgets')
    for name, settings, key in prepared:
        helper.save_provider(name, settings, key)
    prepared.clear()
    command = [sys.executable, ROOT / 'scripts/pricetag/prepare.py', '--hostname', args.hostname,
               '--praxis-image', ids[0], '--maas-image', ids[1], '--metering-image', ids[2],
               '--monthly-usd', str(args.monthly_usd)]
    if args.maas_database_url_file is not None:
        command += ['--maas-database-url-file', args.maas_database_url_file,
                    '--metering-database-url-file', args.metering_database_url_file]
        if args.external_database_ca_file is not None:
            command += ['--external-database-ca-file', args.external_database_ca_file]
    if args.oidc_config is not None:
        command += ['--oidc-config', args.oidc_config]
    if args.tls_bundle is not None:
        command += ['--tls-bundle', args.tls_bundle]
    if not args.sample_users:
        command += ['--no-sample-users']
    if args.mode == 'mock':
        command += ['--mock-provider', '--price-sources', ROOT / 'configs/pricetag/perf-price-sources.json']
        if args.demo_fixture:
            subprocess.run([*prefix, 'podman', 'secret', 'create', 'pricetag-mock-ui-v1', '-'],
                           input=b'synthetic-anthropic', check=True, stdout=subprocess.DEVNULL, cwd='/tmp')
            command += ['--managed-mock', '--mock-secret', 'pricetag-mock-ui-v1']
    else:
        run([sys.executable, ROOT / 'scripts/pricetag/providers', 'export'])
        for option, filename in (('--providers', 'providers.json'), ('--models', 'models.json'),
                                 ('--provider-unit', 'provider-secrets.container'), ('--price-sources', 'prices.json')):
            command += [option, INPUTS / filename]
    run(command, label='Preparing configuration')
    stage(6, 'Start services and verify HTTPS')
    run([ROOT / 'scripts/pricetag/start'], label='Starting PostgreSQL, MaaS, PriceTag and Praxis')
    run(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--cacert',
         DEPLOY / 'ca.pem', 'https://localhost:8443/login', '--output', '/dev/null'])
    print('\nInstallation complete\n' + '=' * 54)
    print('Dashboard: https://' + args.hostname + ':8443/login')
    print('CA: /etc/praxis-pricetag/ca.pem')
    print('Keys: /root/pricetag-admin/' + ('{admin,alice,bob}.key' if args.sample_users else 'admin.key'))
    if not args.sample_users:
        print('Create an inference user next: docs/quickstarts/remote-gateway-pricetag/users.md')
    print('\nNext: open the dashboard, then connect OpenCode (quickstart steps 4–6).')
    print('Guide: docs/quickstarts/remote-gateway-pricetag/README.md')
    if args.mode == 'mock':
        print('Benchmark: sudo python3 scripts/pricetag/benchmark')


def main():
    args = parse_args()
    if args.plan:
        install(args)
        return
    preflight(args.mode)
    os.umask(0o077)
    fd = os.open('/run/pricetag-installer.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another installer is running') from None
        install(args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, EOFError, KeyboardInterrupt) as error:
        print(f'\nInstallation stopped during: {STAGE}', file=sys.stderr)
        print(str(error) or type(error).__name__, file=sys.stderr)
        if LOG_PATH:
            print(f'Details: sudo tail -n 80 {LOG_PATH}', file=sys.stderr)
        print('No files or volumes were automatically deleted. Fix the failed stage and use the manual steps; '
              'if configuration was already prepared, inspect it before running sudo scripts/pricetag/start.', file=sys.stderr)
        sys.exit(1)
    finally:
        if LOG is not None:
            LOG.close()
