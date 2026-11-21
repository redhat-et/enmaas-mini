#!/usr/bin/env python3
"""Prepare an isolated rootless Quadlet deployment; do not start services."""
import argparse
import copy
import ipaddress
import json
import os
import shlex
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from gateway import render
from oidc_config import load as load_oidc
from tls_bundle import validate as validate_tls_bundle

ROOT = Path(__file__).resolve().parents[2]
POSTGRES = 'docker.io/library/postgres@sha256:996e9dc583ba9f0265a2c75547a9c95fb22c251e394749cab205a0a00e1856a1'
PYTHON = 'docker.io/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
EXTERNAL_DATABASE_QUERY_KEYS = {'sslmode', 'hostaddr', 'connect_timeout', 'application_name'}
EXTERNAL_DATABASE_TIMEOUT = 10


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def vllm_only(original, models):
    selected = [m for m in models if m['provider'] == 'vllm']
    if not selected:
        raise ValueError('vLLM-only deployment requires a local vLLM model')
    config = copy.deepcopy(original)
    for chain in config['filter_chains']:
        chain['filters'] = [f for f in chain['filters'] if f['filter'] != 'credential_injection']
        for item in chain['filters']:
            if item['filter'] == 'load_balancer':
                item['clusters'] = [c for c in item['clusters'] if c['name'] == 'vllm']
    return config, selected


def mock_inputs(managed=False):
    if managed:
        from demo_config import mock_configuration, native_inputs
        document = mock_configuration()
        return native_inputs(document), document['models']
    models = [{'id': 'fixture', 'provider': 'vllm', 'model': 'fixture',
               'apis': ['openai', 'anthropic'], 'context': 32768, 'output': 4096}]
    config = {'filter_chains': [{'name': api, 'filters': [
        {'filter': 'headers', 'request_remove': ['Authorization', 'X-Api-Key']},
        {'filter': 'load_balancer', 'clusters': [{'name': 'vllm',
         'endpoints': ['pricetag-provider:' + port]}]}]}
        for api, port in [('openai', '18080'), ('anthropic', '18081')]]}
    return config, models


def postgres_bootstrap(metering_password, maas_password):
    """Create the application roles/databases idempotently through psql."""
    statements = []
    for role, password, database in (
            ('metering', metering_password, 'metering'),
            ('maas', maas_password, 'maas')):
        statements += [
            f"SELECT format('CREATE ROLE {role} LOGIN PASSWORD %L', {literal(password)}) "
            f"WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={literal(role)}) \\gexec",
            f'ALTER ROLE {role} LOGIN PASSWORD {literal(password)};',
            f"SELECT 'CREATE DATABASE {database} OWNER {role}' "
            f"WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname={literal(database)}) \\gexec",
        ]
    return '\n'.join(statements) + '\n'


def validate_external_database_url(url_text, ca_file=None, ca_required=True):
    """Validate a private application URI without logging its credentials."""
    parsed = urlsplit(url_text.strip())
    if parsed.scheme not in ('postgres', 'postgresql'):
        raise ValueError('external database URL must use postgres:// or postgresql://')
    if parsed.fragment:
        raise ValueError('external database URL must not include a fragment')
    if not parsed.hostname or not parsed.port or not parsed.username or not parsed.password:
        raise ValueError('external database URL must include host, port, username and password')
    if not 1 <= parsed.port <= 65535:
        raise ValueError('external database URL port must be between 1 and 65535')
    database = parsed.path.lstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', database):
        raise ValueError('external database URL must name a single maintenance database')
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query_keys = [key for key, _ in query_items]
    if len(query_keys) != len(set(query_keys)):
        raise ValueError('external database URL must not repeat query parameters')
    unsupported = [key for key in query_keys if key not in EXTERNAL_DATABASE_QUERY_KEYS]
    if unsupported:
        raise ValueError('external database URL contains unsupported query parameters: ' + ', '.join(sorted(set(unsupported))))
    query = dict(query_items)
    username = unquote(parsed.username)
    password = unquote(parsed.password)
    for field, value in (('username', username), ('password', password),
                         ('host', parsed.hostname), ('database', database)):
        if any(unicodedata.category(character) == 'Cc' for character in value):
            raise ValueError(f'external database URL {field} must not contain control characters')
    for key, value in query_items:
        if any(unicodedata.category(character) == 'Cc' for character in value):
            raise ValueError(f'external database URL {key} query parameter must not contain control characters')
    connect_timeout = query.get('connect_timeout')
    if connect_timeout is not None and (re.fullmatch(r'[0-9]{1,2}', connect_timeout) is None
                                        or not 1 <= int(connect_timeout) <= 30):
        raise ValueError('external database URL connect_timeout must be an integer from 1 to 30 seconds')
    hostaddr = query.get('hostaddr')
    if 'hostaddr' in query:
        try:
            ipaddress.ip_address(hostaddr)
        except ValueError as error:
            raise ValueError('external database URL hostaddr must be an IPv4 or IPv6 address') from error
    sslmode = query.get('sslmode', '').lower()
    if sslmode != 'verify-full':
        raise ValueError('external database URL must set sslmode=verify-full to verify server identity')
    if sslmode == 'verify-full' and ca_required:
        if ca_file is None:
            raise ValueError('sslmode=verify-full requires --external-database-ca-file')
        if not ca_file.is_absolute() or not ca_file.is_file():
            raise ValueError('external database CA file must be an existing absolute path')
    elif ca_file is not None:
        raise ValueError('external database CA file requires sslmode=verify-full')
    return {
        'scheme': parsed.scheme,
        'host': parsed.hostname,
        'hostaddr': hostaddr,
        'port': parsed.port,
        'username': unquote(parsed.username),
        'password': unquote(parsed.password),
        'database': database,
        'sslmode': sslmode,
        'connect_timeout': connect_timeout,
        'query': [(key, value.lower() if key == 'sslmode' else value) for key, value in query_items],
    }


def external_database_server(config):
    """Return the effective server address and port for a validated URL."""
    return config['hostaddr'] or config['host'], config['port']


def validate_external_database_urls(maas_url, metering_url, ca_file=None):
    """Validate both application URLs and their shared database server."""
    external_database = {
        'maas': validate_external_database_url(maas_url, ca_file),
        'metering': validate_external_database_url(metering_url, ca_file),
    }
    if external_database['maas']['hostaddr'] is not None:
        raise ValueError('external maas URL must not use hostaddr')
    for name, config in external_database.items():
        if config['database'] != name or config['username'] != name:
            raise ValueError(f'external {name} URL must target the {name} database and use the {name} role')
    if external_database_server(external_database['maas']) != \
            external_database_server(external_database['metering']):
        raise ValueError('external maas and metering URLs must use the same effective database server address and port')
    return external_database


def external_application_url(url_text, ca_configured):
    """Render the normalized private URI consumed by an application."""
    config = validate_external_database_url(url_text, ca_required=False)
    query = list(config['query'])
    if ca_configured:
        query.append(('sslrootcert', '/etc/pricetag-external-ca.crt'))
    host = f'[{config["host"]}]' if ':' in config['host'] else config['host']
    authority = (f'{quote(config["username"], safe="")}:{quote(config["password"], safe="")}'
                 f'@{host}:{config["port"]}')
    return urlunsplit((config['scheme'], authority, '/' + config['database'], '&'.join(
        f'{quote(key, safe="")}={quote(value, safe="")}' for key, value in query), ''))


def external_client_environment(url_text, expected_database, ca_configured):
    """Render the psql environment for the same target as the application."""
    config = validate_external_database_url(url_text, ca_required=False)
    if config['database'] != expected_database:
        raise ValueError(f'external {expected_database} URL must target the {expected_database} database')
    if config['username'] != expected_database:
        raise ValueError(f'external {expected_database} URL must use the {expected_database} role')
    values = {
        'PGHOST': config['host'],
        **({'PGHOSTADDR': config['hostaddr']} if config['hostaddr'] else {}),
        'PGPORT': str(config['port']),
        'PGUSER': config['username'],
        'PGDATABASE': config['database'],
        'PGSSLMODE': config['sslmode'],
        'PGCONNECT_TIMEOUT': config['connect_timeout'] or str(EXTERNAL_DATABASE_TIMEOUT),
    }
    if ca_configured:
        values['PGSSLROOTCERT'] = '/etc/pricetag-external-ca.crt'
    return values


def external_password_file(password):
    """Render a private shell fragment containing the database password."""
    if '\x00' in password:
        raise ValueError('external database password must not contain a NUL byte')
    return 'export PGPASSWORD=' + shlex.quote(password) + '\n'


def gateway_quadlet_body(image, provider_secrets, mount, mock_provider):
    mock_network = 'Network=pricetag-mock.network\n' if mock_provider else ''
    return (
        f'Image={image}\nPull=never\nNetworkAlias=pricetag-gateway\n'
        + mock_network
        + 'Secret=pricetag-m2m,type=mount,target=/run/secrets/pricetag-m2m,uid=1001,gid=1001,mode=0400\n'
        + 'PublishPort=0.0.0.0:8443:8443\n'
        + '\n'.join(provider_secrets) + '\n'
        + mount + 'Exec=-c /etc/praxis/gateway.json\n'
    )


def main():
    """Render all private deployment files without starting services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers', type=Path, default=Path('/etc/praxis/shared-gateway.yaml'))
    parser.add_argument('--models', type=Path, default=Path('/etc/praxis/unified-models.json'))
    parser.add_argument('--provider-unit', type=Path)
    parser.add_argument('--praxis-image', required=True, help='local immutable image ID containing MaaS auth and metering filters')
    parser.add_argument('--maas-image', required=True, help='local immutable cluster-free MaaS API image ID')
    parser.add_argument('--metering-image', required=True, help='local image ID sha256:... in praxis-svc storage')
    parser.add_argument('--hostname', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--vllm-only', action='store_true',
                        help='expose only local vLLM; do not mount cloud provider secrets')
    mode.add_argument('--mock-provider', action='store_true',
                      help='isolated synthetic provider for a fresh performance-test VM')
    parser.add_argument('--mock-profiles', action='store_true', help='mock standard replaceable provider profiles')
    parser.add_argument('--managed-mock', action='store_true', help='authenticated mock providers for the management demo')
    parser.add_argument('--mock-secret', default='pricetag-mock-ui-v1', help='existing synthetic credential Podman secret')
    parser.add_argument('--sample-users', action=argparse.BooleanOptionalAction, default=True,
                        help='create sample users on start; admin is always created')
    parser.add_argument('--monthly-usd', type=int, default=5)
    parser.add_argument('--maas-database-url-file', type=Path,
                        help='private file containing a URL to the existing external maas database')
    parser.add_argument('--metering-database-url-file', type=Path,
                        help='private file containing a URL to the existing external metering database')
    parser.add_argument('--external-database-ca-file', type=Path,
                        help='CA certificate used when the external URL sets sslmode=verify-full')
    parser.add_argument('--tls-bundle', type=Path, help='pre-issued server.pem, server.key and ca.pem; never a CA signing key')
    parser.add_argument('--oidc-config', type=Path, help='private root-owned JSON for optional external browser SSO')
    parser.add_argument('--price-sources', type=Path, required=True,
                        help='JSON mapping gateway aliases to reviewed PriceTag pricing model IDs')
    parser.add_argument('--directory', type=Path, default=Path('/etc/praxis-pricetag'))
    parser.add_argument('--admin-directory', type=Path, default=Path('/root/pricetag-admin'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root on the RHEL host')
    if args.mock_profiles and (not args.mock_provider or args.managed_mock or args.provider_unit is None):
        parser.error('--mock-profiles requires --mock-provider and --provider-unit, without --managed-mock')
    if args.managed_mock and not args.mock_provider:
        parser.error('--managed-mock requires --mock-provider')
    if args.managed_mock and not re.fullmatch(r'pricetag-mock-ui-v[0-9]+', args.mock_secret):
        parser.error('managed mock requires a versioned synthetic secret reference')
    if not args.mock_provider and args.provider_unit is None:
        parser.error('--provider-unit is required unless --mock-provider is selected')
    if not all(re.fullmatch(r'sha256:[0-9a-f]{64}', image)
               for image in (args.metering_image, args.praxis_image, args.maas_image)):
        parser.error('image arguments must be immutable local sha256 image IDs')
    if not 1 <= args.monthly_usd <= 10000:
        parser.error('monthly-usd must be 1–10000')
    if args.maas_database_url_file is not None or args.metering_database_url_file is not None:
        if args.maas_database_url_file is None or args.metering_database_url_file is None:
            parser.error('external database installation requires both --maas-database-url-file and --metering-database-url-file')
        if not args.maas_database_url_file.is_absolute() or not args.metering_database_url_file.is_absolute():
            parser.error('external database URL file paths must be absolute')
        if args.external_database_ca_file is not None and not args.external_database_ca_file.is_absolute():
            parser.error('--external-database-ca-file must be an absolute path')
    elif args.external_database_ca_file is not None:
        parser.error('--external-database-ca-file requires both external database URL files')
    tls_files = validate_tls_bundle(args.tls_bundle, args.hostname) if args.tls_bundle else None
    oidc_env, oidc_ca, oidc_host = load_oidc(args.oidc_config, f'https://{args.hostname}:8443') if args.oidc_config else ({}, None, None)
    for directory in (args.directory, args.admin_directory):
        if not directory.is_absolute() or re.search(r'[\s%]', str(directory)) or directory.exists():
            parser.error('use new absolute directories without whitespace or percent signs')
    owner = pwd.getpwnam('praxis-svc')
    if args.mock_profiles:
        from mock_profiles import configuration
        from demo_config import native_inputs
        document = configuration()
        original, models = native_inputs(document), document['models']
    elif args.mock_provider:
        original, models = mock_inputs(managed=args.managed_mock)
    else:
        models = json.loads(args.models.read_text())
        original = json.loads(args.providers.read_text())
    if args.vllm_only:
        original, models = vllm_only(original, models)
    config = render(original, models,
                    list(dict.fromkeys([f'https://{args.hostname}:8443', 'https://localhost:8443'])))
    sources = json.loads(args.price_sources.read_text())
    if not isinstance(sources, dict) or any(not isinstance(v, str) for v in sources.values()):
        parser.error('price-sources must map aliases to model names')
    if not args.mock_provider and any(m['id'] == sources.get(m['id'], m['model']) for m in models):
        parser.error('use distinct gateway aliases to keep their initial pricing snapshot stable')
    provider_secrets = [f'Secret={args.mock_secret},type=env,target=CUSTOM_MOCK_API_KEY'] if args.managed_mock else []
    for line in ([] if args.mock_provider and not args.mock_profiles else args.provider_unit.read_text().splitlines()):
        if re.fullmatch(r'Secret=[A-Za-z0-9_.-]+,type=env,target=[A-Z0-9_]+_API_KEY', line):
            provider_secrets.append(line)
    needed = {entry['env_var'] for chain in config['filter_chains'] for f in chain['filters']
              if f['filter'] == 'credential_injection' for entry in f['clusters']
              if 'env_var' in entry and entry['env_var'] != 'M2M_SHARED_SECRET'}
    supplied = {line.rsplit('=', 1)[1] for line in provider_secrets}
    if not needed <= supplied:
        parser.error('provider Quadlet does not supply every required API-key secret')
    provider_secrets = [line for line in provider_secrets if line.rsplit('=', 1)[1] in needed]
    external_database = None
    if args.maas_database_url_file is not None:
        try:
            maas_url = args.maas_database_url_file.read_text().strip()
            metering_url = args.metering_database_url_file.read_text().strip()
            external_database = validate_external_database_urls(
                maas_url, metering_url, args.external_database_ca_file)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    os.umask(0o077)
    args.admin_directory.mkdir(mode=0o700)
    args.directory.mkdir(mode=0o750)
    bundle = args.directory / 'gateway'
    units = args.directory / 'quadlets'
    bundle.mkdir(mode=0o750)
    units.mkdir(mode=0o750)
    credential = ROOT / 'scripts/remote-gateway/credentials'

    def credentials(*options):
        subprocess.run([str(credential), *map(str, options)], check=True, stdout=subprocess.DEVNULL)

    if tls_files is None:
        credentials('lab-tls', '--directory', args.admin_directory / 'tls', '--hostname', args.hostname,
                    '--alt-hostname', 'localhost', '--alt-hostname', '127.0.0.1')
    else:
        (args.admin_directory / 'tls').mkdir(mode=0o700)
        for source, target in (('server.pem', 'server.pem'), ('server.key', 'server-key.pem'), ('ca.pem', 'ca.pem')):
            shutil.copyfile(tls_files[source], args.admin_directory / 'tls' / target)
    for source, target in [(args.admin_directory / 'tls/server.pem', 'tls.pem'),
                           (args.admin_directory / 'tls/server-key.pem', 'tls-key.pem')]:
        shutil.copyfile(source, bundle / target)
    (bundle / 'gateway.json').write_text(json.dumps(config, indent=2) + '\n')
    shutil.copyfile(args.admin_directory / 'tls/ca.pem', args.directory / 'ca.pem')
    if external_database is None:
        postgres_password, metering_password, maas_password = (
            secrets.token_urlsafe(32) for _ in range(3))
    else:
        postgres_password = metering_password = maas_password = ''

    def env(name, values):
        (args.directory / name).write_text(''.join(f'{k}={v}\n' for k, v in values.items()))

    initdb = args.directory / 'initdb'
    if external_database is None:
        initdb.mkdir(mode=0o750)
        (initdb / '10-create-databases.sql').write_text(
            postgres_bootstrap(metering_password, maas_password))
        env('postgres.env', {'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'postgres',
                            'POSTGRES_PASSWORD': postgres_password, 'TZ': 'UTC', 'PGTZ': 'UTC'})
    else:
        ca_configured = args.external_database_ca_file is not None
        if ca_configured:
            shutil.copyfile(args.external_database_ca_file, args.directory / 'external-ca.crt')
        env('external-metering-db.env', external_client_environment(metering_url, 'metering', ca_configured))
        env('external-maas-db.env', external_client_environment(maas_url, 'maas', ca_configured))
        (args.directory / 'external-metering-db.password').write_text(
            external_password_file(external_database['metering']['password']))
        (args.directory / 'external-maas-db.password').write_text(
            external_password_file(external_database['maas']['password']))
        metering_url = external_application_url(metering_url, ca_configured)
        maas_url = external_application_url(maas_url, ca_configured)
    m2m_secret = secrets.token_urlsafe(48)
    management_secret = secrets.token_urlsafe(48)
    if oidc_env:
        partner_secret = secrets.token_urlsafe(48)
        oidc_env['USER_MANAGEMENT_API_SECRET'] = partner_secret
        (args.directory / 'partner-secret').write_text(partner_secret + '\n')
    metering_url = (metering_url if external_database else
                    f'postgres://metering:{metering_password}@pricetag-db:5432/metering?sslmode=disable')
    env('metering.env', {'DATABASE_URL': metering_url,
        'M2M_AUTH_REQUIRED': 'true', 'M2M_SHARED_SECRET': m2m_secret,
        'SESSION_SECRET': secrets.token_urlsafe(48),
        'MANAGEMENT_API_SECRET': management_secret,
        'MANAGEMENT_PUBLIC_ORIGIN': f'https://{args.hostname}:8443',
        'WELCOME_GATEWAY_URL': f'https://{args.hostname}:8443',
        'WELCOME_DASHBOARD_URL': f'https://{args.hostname}:8443',
        'DEFAULT_KEY_EXPIRY_HOURS': '2160', 'MAX_KEY_EXPIRY_HOURS': '2160',
        'GATEWAY_MODELS_FILE': '/etc/gateway-catalog/models.json',
        'KEY_SERVICE_URL': 'http://pricetag-maas:8080',
        'KEY_SERVICE_USER_HEADER': 'X-MaaS-Username',
        'KEY_SERVICE_GROUPS_HEADER': 'X-MaaS-Group',
        'PARTNER_USER_KEY_GROUP': 'system:authenticated',
        'SUPERADMIN_USERS': 'admin', 'MONTHLY_TOKEN_QUOTA': '10000000000', 'TZ': 'UTC',
        'MAAS_VALIDATE_URL': 'http://pricetag-maas:8080/internal/v1/api-keys/validate', **oidc_env})
    if oidc_ca is not None:
        shutil.copyfile(oidc_ca, args.directory / 'oidc-ca.pem')
    (args.directory / 'm2m-secret').write_text(m2m_secret + '\n')
    maas_url = (maas_url if external_database else
                f'postgres://maas:{maas_password}@pricetag-db:5432/maas?sslmode=disable')
    (args.directory / 'maas-db-url').write_text(maas_url + '\n')
    (args.directory / 'sample-users').write_text('yes\n' if args.sample_users else 'no\n')
    (args.directory / 'admin-directory').write_text(str(args.admin_directory) + '\n')
    (args.directory / 'management-secret').write_text(management_secret + '\n')
    (args.directory / 'bootstrap.json').write_text(json.dumps({'models': models, 'sources': sources, 'monthly_usd': args.monthly_usd}) + '\n')
    (args.directory / 'models.json').write_text(json.dumps(models, indent=2) + '\n')
    catalog = args.directory / 'catalog'
    catalog.mkdir(mode=0o750)
    (catalog / 'models.json').write_text(json.dumps([{k: m[k] for k in ('id', 'model', 'provider')} for m in models]) + '\n')
    (args.directory / 'price-sources.json').write_text(json.dumps(sources, indent=2) + '\n')
    (units / 'pricetag.network').write_text(
        '[Network]\nNetworkName=pricetag-private\nDriver=bridge\nSubnet=10.77.0.0/24\n')
    common = 'ReadOnly=true\nNoNewPrivileges=true\n'
    app = common + 'User=1001\nGroup=1001\nUserNS=keep-id:uid=1001,gid=1001\nDropCapability=all\n'
    mount = f'Volume={bundle}:/etc/praxis:ro\n'

    def container(name, description, dependencies, body, memory, network='pricetag.network'):
        (units / (name + '.container')).write_text(
            f'[Unit]\nDescription={description}\nWants=network-online.target\nAfter=network-online.target {dependencies}\n'
            f'Requires={dependencies}\n\n[Container]\nContainerName={name}\nNetwork={network}\n'
            f'{body}\n[Service]\nRestart=on-failure\nRestartSec=5\nTimeoutStartSec=900\n'
            f'MemoryMax={memory}\n\n[Install]\nWantedBy=default.target\n')

    database_dependency = '' if external_database else 'pricetag-db.service'
    application_dependency = database_dependency or 'pricetag-network.service'
    external_ca_mount = (f'Volume={args.directory}/external-ca.crt:/etc/pricetag-external-ca.crt:ro\n'
                         if external_database and ca_configured else '')
    if external_database is None:
        (units / 'pricetag-db.volume').write_text('[Volume]\nVolumeName=pricetag-data\n')
        container('pricetag-db', 'MaaS and PriceTag PostgreSQL', '',
            f'Image={POSTGRES}\nEnvironmentFile={args.directory}/postgres.env\n'
            'Volume=pricetag-db.volume:/var/lib/postgresql/data\n'
            'Tmpfs=/var/run/postgresql:rw,mode=3775\nTmpfs=/tmp\n'
            'HealthCmd=pg_isready -U postgres -d postgres\nHealthInterval=10s\n' + common, '1G')
    container('pricetag-maas', 'MaaS standalone key API', application_dependency,
        f'Image={args.maas_image}\nPull=never\nNetworkAlias=pricetag-maas\n'
        'IP=10.77.0.10\n'
        'PublishPort=127.0.0.1:18086:8080\n'
        f'Volume={args.directory}/maas-db-url:/run/secrets/maas-db-url:ro\n'
        + external_ca_mount +
        'Environment=MAAS_RUNTIME_MODE=standalone\nEnvironment=MAAS_SUBSCRIPTION_MODE=standalone\n'
        'Environment=ADDRESS=:8080\nEnvironment=SECURE=false\n'
        'Environment=DB_CONNECTION_URL_FILE=/run/secrets/maas-db-url\n'
        'Environment=API_KEY_MAX_EXPIRATION_DAYS=90\nEnvironment=MAAS_ADMIN_GROUPS=single-server-admin\n'
        'Environment=MAAS_TRUSTED_PROXY_CIDRS=127.0.0.1/32,10.77.0.10/32,10.77.0.11/32,10.77.0.12/32\n'
        'Environment=METRICS_SECURE=false\nEnvironment=METRICS_ADDRESS=127.0.0.1:9090\n' + app, '1G')
    container('pricetag-metering', 'PriceTag metering and dashboards', application_dependency,
        f'Image={args.metering_image}\nPull=never\nNetworkAlias=pricetag-metering\n'
        f'EnvironmentFile={args.directory}/metering.env\nPublishPort=127.0.0.1:18087:8080\n'
        + (f'AddHost={oidc_host}\n' if oidc_host is not None else '') +
        f'Volume={args.directory}/catalog:/etc/gateway-catalog:ro\n' + external_ca_mount +
        (f'Volume={args.directory}/oidc-ca.pem:/etc/pricetag-oidc-ca.pem:ro\n' if oidc_ca is not None else '') + app, '2G',
        network='pricetag.network:ip=10.77.0.12')
    dependencies = 'pricetag-metering.service'
    if args.mock_provider:
        (units / 'pricetag-mock.network').write_text(
            '[Network]\nNetworkName=pricetag-mock\nDriver=bridge\nSubnet=10.78.0.0/24\n')
        fixtures = args.directory / 'fixtures'
        fixtures.mkdir()
        shutil.copyfile(ROOT / 'tests/common/provider.py', fixtures / 'provider.py')
        shutil.copyfile(ROOT / 'tests/pricetag/provider.py', fixtures / 'run.py')
        if args.mock_profiles:
            (fixtures / 'mock-profiles.json').write_text(json.dumps(document))
        container('pricetag-provider', 'Synthetic LLM for performance testing only', '',
            f'Image={PYTHON}\nVolume={fixtures}:/fixtures:ro\nEnvironment=MOCK_STREAM_DELAY=1\n' +
            ('Environment=MOCK_MANAGEMENT=1\n' if args.managed_mock else '') +
            ('Environment=MOCK_PROFILES=1\n' if args.mock_profiles else '') +
            'Exec=python3 /fixtures/run.py\n' + app, '1G', network='pricetag-mock.network')
        dependencies += ' pricetag-provider.service'
    container('pricetag-gateway', 'Praxis HTTPS gateway with MaaS identity and USD budgets',
        'pricetag-metering.service pricetag-maas.service',
        gateway_quadlet_body(args.praxis_image, provider_secrets, mount, args.mock_provider) + app,
        '1G', network='pricetag.network:ip=10.77.0.11')
    for path in [args.directory, *args.directory.rglob('*')]:
        os.chown(path, 0, owner.pw_gid)
        path.chmod(0o750 if path.is_dir() else 0o640)
    (args.directory / 'management-secret').chmod(0o600)
    if oidc_env:
        (args.directory / 'partner-secret').chmod(0o600)
    print(f'Prepared {args.directory}; no services started. Administrator credentials: {args.admin_directory}')


if __name__ == '__main__':
    main()
