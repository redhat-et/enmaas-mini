#!/usr/bin/env python3
"""Disposable loopback-only SSO/telemetry fixture; no AWS or host trust changes."""
import argparse
import base64
import json
import os
import re
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import uuid
import yaml
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INSTANCE = os.environ.get('ENMAAS_LAB_INSTANCE', 'external-local')
if not re.fullmatch(r'[a-z][a-z0-9-]{2,31}', INSTANCE):
    raise ValueError('Invalid local lab instance name')
STATE = ROOT/'.state'/('external-services-local' if INSTANCE == 'external-local' else INSTANCE)
POD = 'enmaas-' + INSTANCE
LABEL = 'io.enmaas.test-lab=' + INSTANCE
PORTS = (18443, 19443, 24443)
IMAGES = {name: entry['image'] for name, entry in json.loads((HERE/'images.json').read_text()).items()}
PERSES_ASSETS = ('project.json', 'datasource.json', 'dashboard.json', 'gateway-dashboard.json',
                 'host-dashboard.json', 'tempo-datasource.json', 'traces-dashboard.json', 'trace-detail-dashboard.json', 'loki-datasource.json', 'logs-dashboard.json')


def privacy_transform():
    resource=['keep_keys(attributes, ["service.name", "service.version", "deployment.environment.name"])']
    scope=['set(name, "enmaas")','set(version, "")','keep_keys(attributes, [])']
    attributes='keep_keys(attributes, ["http.request.method", "http.response.status_code", "error.type", "gen_ai.provider.name", "gen_ai.request.model"])'
    transform={'error_mode':'propagate',
        'log_statements':[{'context':'resource','statements':resource},{'context':'scope','statements':scope},
            {'context':'log','statements':[attributes,'set(body, "gateway event")','set(severity_text, "")']}],
        'trace_statements':[{'context':'resource','statements':resource},{'context':'scope','statements':scope},
            {'context':'span','statements':[attributes,'set(name, "request")','set(status.message, "")','set(trace_state, "")','set(links, nil)']},
            {'context':'spanevent','statements':['set(name, "event")','keep_keys(attributes, [])']}],
        'metric_statements':[{'context':'resource','statements':resource},{'context':'scope','statements':scope},
            {'context':'metric','statements':['set(description, "")','set(unit, "")']},
            {'context':'datapoint','statements':['keep_keys(attributes, ["http.request.method", "http.response.status_code", "gen_ai.request.model", "state", "cpu", "device", "direction", "type", "cluster", "route", "listener", "endpoint", "method", "status_class", "filter", "phase", "quantile", "le", "result", "reason"])','set(exemplars, nil) where metric.type != METRIC_DATA_TYPE_SUMMARY']}]}
    return transform


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def pod_args(gateway=False):
    args = ['podman', 'pod', 'create', '--name', POD, '--label', LABEL]
    for port in (*PORTS, *((28443,) if gateway else ())):
        args += ['--publish', f'127.0.0.1:{port}:{port}']
    return args


def write(root, name, value):
    path = root/name
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2)+'\n')
    path.chmod(0o600)


def configure(root, certificates=True, authorization_test=False):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    if (root/'credentials.json').exists():
        raise FileExistsError('Existing lab credentials; use start to resume, never overwrite them')
    credentials = {key: secrets.token_urlsafe(30) for key in ('admin', 'alice', 'bob', 'db', 'oidc', 'gateway_oidc')}
    write(root, 'credentials.json', credentials)
    identities = {name: str(uuid.uuid4()) for name in ('admin', 'alice', 'bob')}
    write(root, 'identities.json', identities)
    users = []
    for name in ('admin', 'alice', 'bob'):
        users.append({'id': identities[name], 'username': name, 'email': f'{name}@example.test', 'emailVerified': True,
                      'enabled': True, 'firstName': name.title(), 'lastName': 'Lab',
                      'groups': ['/observability-admin'] if name == 'admin' else [],
                      'credentials': [{'type': 'password', 'value': credentials[name], 'temporary': False}]})
    write(root, 'realm.json', {
        'realm': 'enmaas-lab', 'enabled': True, 'sslRequired': 'all',
        'registrationAllowed': False, 'resetPasswordAllowed': False,
        'bruteForceProtected': True, 'permanentLockout': False, 'maxTemporaryLockouts': 0,
        'failureFactor': 5, 'waitIncrementSeconds': 60,
        'minimumQuickLoginWaitSeconds': 60, 'quickLoginCheckMilliSeconds': 1000,
        'maxFailureWaitSeconds': 900, 'maxDeltaTimeSeconds': 43200,
        'accessTokenLifespan': 60, 'ssoSessionIdleTimeout': 300, 'ssoSessionMaxLifespan': 1800,
        'groups': [{'name': 'observability-admin'}], 'users': users,
        'clients': [{'clientId': 'observability', 'secret': credentials['oidc'],
                     'publicClient': False, 'standardFlowEnabled': True,
                     'directAccessGrantsEnabled': False,
                     'redirectUris': ['https://localhost:19443/oauth2/callback'],
                     'webOrigins': ['https://localhost:19443'],
                     'protocolMappers': [{'name': 'groups', 'protocol': 'openid-connect',
                        'protocolMapper': 'oidc-group-membership-mapper',
                        'config': {'claim.name': 'groups', 'full.path': 'true',
                                   'id.token.claim': 'true', 'access.token.claim': 'true',
                                   'userinfo.token.claim': 'true'}}]}]})
    realm = json.loads((root/'realm.json').read_text())
    realm['clients'].append({
        'clientId': 'enmaas-mini', 'secret': credentials['gateway_oidc'],
        'publicClient': False, 'standardFlowEnabled': True,
        'directAccessGrantsEnabled': False,
        'redirectUris': ['https://localhost:28443/oauth/callback'],
        'webOrigins': ['https://localhost:28443'],
        'attributes': {'pkce.code.challenge.method': 'S256',
                       'post.logout.redirect.uris': 'https://localhost:28443/login'},
        # Map Keycloak's immutable user ID, never email or a user-editable attribute.
        'protocolMappers': [{
            'name': 'metering-identity', 'protocol': 'openid-connect',
            'protocolMapper': 'oidc-usermodel-property-mapper',
            'config': {'user.attribute': 'id', 'claim.name': 'rhatUUID',
                       'jsonType.label': 'String', 'id.token.claim': 'true',
                       'access.token.claim': 'true', 'userinfo.token.claim': 'true'}}]})
    if authorization_test:
        # Explicitly opt in only for a disposable local realm. RHEL generation
        # never enables this client; the credential stays in private lab state.
        credentials['authorization_test']=secrets.token_urlsafe(32)
        write(root, 'credentials.json', credentials)
        realm['clients'].append({
            'clientId':'lab-authorization-test', 'secret':credentials['authorization_test'],
            'publicClient':False, 'serviceAccountsEnabled':True,
            'defaultClientScopes':['kc-admin-api'],
            'standardFlowEnabled':False, 'directAccessGrantsEnabled':False})
        realm['users'].append({
            'username':'service-account-lab-authorization-test','enabled':True,
            'serviceAccountClientId':'lab-authorization-test',
            'clientRoles':{'realm-management':['manage-users','query-groups','query-users']}})
    write(root, 'realm.json', realm)
    write(root, 'gateway-oidc.json', {
        'issuer_url': 'https://localhost:18443/realms/enmaas-lab',
        'client_id': 'enmaas-mini', 'client_secret': credentials['gateway_oidc'],
        'ca_file': str((root/'ca.pem').resolve())})
    write(root, 'db-password', credentials['db'])
    write(root, 'keycloak.conf', f'''db=postgres
 cache=local
 db-url=jdbc:postgresql://localhost:5432/keycloak
 db-username=keycloak
 db-password={credentials['db']}
 hostname=https://localhost:18443
 http-enabled=false
 https-port=18443
 https-certificate-file=/lab/server.pem
 https-certificate-key-file=/lab/server.key
 health-enabled=true
'''.replace('\n ', '\n'))
    write(root, 'oauth.cfg', f'''provider = "keycloak-oidc"
oidc_issuer_url = "https://localhost:18443/realms/enmaas-lab"
client_id = "observability"
client_secret = "{credentials['oidc']}"
redirect_url = "https://localhost:19443/oauth2/callback"
provider_ca_files = ["/lab/ca.pem"]
allowed_groups = ["/observability-admin"]
email_domains = ["example.test"]
scope = "openid profile email"
code_challenge_method = "S256"
https_address = "0.0.0.0:19443"
http_address = ""
tls_cert_file = "/lab/server.pem"
tls_key_file = "/lab/server.key"
upstreams = ["http://127.0.0.1:8080/"]
cookie_secret = "{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}"
cookie_secure = true
cookie_httponly = true
cookie_samesite = "lax"
cookie_expire = "5m"
cookie_refresh = "1m"
reverse_proxy = false
skip_provider_button = true
request_logging = false
auth_logging = false
''')
    write(root, 'perses.yaml', f'''database:
  file:
    folder: /perses
    extension: json
security:
  enable_auth: false
  encryption_key: {secrets.token_urlsafe(24)}
datasource:
  disable_local: true
  proxy:
    allowed_hosts: ['127.0.0.1']
    allowed_networks: ['127.0.0.1/32']
    http:
      max_conns_per_host: 16
      max_timeout: 10s
provisioning:
  folders: [/etc/perses/lab]
''')
    write(root, 'project.json', json.loads((HERE/'perses/project.json').read_text()))
    write(root, 'datasource.json', {
        'kind': 'GlobalDatasource', 'metadata': {'name': 'prometheus'},
        'spec': {'default': True, 'plugin': {'kind': 'PrometheusDatasource',
            'spec': {'proxy': {'kind': 'HTTPProxy', 'spec': {'url': 'http://127.0.0.1:9090'}}}}}})
    panel = json.loads((HERE/'perses/dashboards/filter-latency.json').read_text())['spec']['panels']['0']
    panel['spec']['display'] = {'name': 'Local test requests', 'description': 'Synthetic transport qualification, not gateway usage.'}
    panel['spec'].pop('links', None)
    panel['spec']['plugin']['spec'].pop('yAxis', None)
    panel['spec']['queries'][0]['spec']['plugin']['spec'] = {'query': 'enmaas_lab_requests_total'}
    write(root, 'dashboard.json', {
        'kind': 'Dashboard', 'metadata': {'name': 'local-transport', 'project': 'praxis'},
        'spec': {'display': {'name': 'Local transport test'}, 'duration': '15m',
            'panels': {'0': panel}, 'layouts': [{'kind': 'Grid', 'spec': {'items': [
                {'x': 0, 'y': 0, 'width': 24, 'height': 10, 'content': {'$ref': '#/spec/panels/0'}}]}}]}})
    for name in ('gateway-dashboard.json','host-dashboard.json','tempo-datasource.json','traces-dashboard.json','trace-detail-dashboard.json', 'loki-datasource.json', 'logs-dashboard.json'):
        write(root, name, json.loads((HERE/'perses'/name).read_text()))
    write(root, 'prometheus.yaml', '''global:
  scrape_interval: 5s
scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['127.0.0.1:9090']
  - job_name: collector
    static_configs:
      - targets: ['127.0.0.1:8889']
''')
    write(root, 'loki.yaml', '''auth_enabled: false
server:
  http_listen_port: 3100
common:
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
storage_config:
  filesystem:
    directory: /loki/chunks
limits_config:
  retention_period: 24h
compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
''')
    write(root, 'tempo.yaml', '''server:
  http_listen_port: 3200
  grpc_listen_port: 9096
distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 127.0.0.1:4319
storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
backend_scheduler:
  provider:
    compaction:
      compaction:
        block_retention: 24h
usage_report:
  reporting_enabled: false
''')
    write(root, 'collector.yaml', '''receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:24443
        max_request_body_size: 4194304
        tls:
          cert_file: /lab/server.pem
          key_file: /lab/server.key
          client_ca_file: /lab/ca.pem
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 128
    spike_limit_mib: 32
  batch:
    timeout: 5s
    send_batch_size: 256
    send_batch_max_size: 512
exporters:
  prometheus:
    endpoint: 127.0.0.1:8889
  otlphttp/loki:
    endpoint: http://127.0.0.1:3100/otlp
  otlphttp/tempo:
    endpoint: http://127.0.0.1:4319
service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheus]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, transform, batch]
      exporters: [otlphttp/loki]
    traces:
      receivers: [otlp]
      processors: [memory_limiter, transform, batch]
      exporters: [otlphttp/tempo]
''')
    collector = yaml.safe_load((root/'collector.yaml').read_text())
    collector['processors']['transform'] = privacy_transform()
    for pipeline in collector['service']['pipelines'].values():
        pipeline['processors'] = ['memory_limiter', 'transform', 'batch']
    write(root, 'collector.yaml', yaml.safe_dump(collector, sort_keys=False))
    if certificates:
        issue_certificates(root)


def issue_certificates(root):
    def openssl(*args):
        command(['openssl', *args], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '7',
            '-subj', '/CN=enMAAS disposable local lab CA', '-keyout', 'ca.key', '-out', 'ca.pem',
            '-addext', 'subjectKeyIdentifier=hash', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    for name, purpose in (('server', 'serverAuth'), ('client', 'clientAuth')):
        write(root, f'{name}.ext', f'basicConstraints=critical,CA:FALSE\nextendedKeyUsage={purpose}\n'
              'keyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n')
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', f'/CN={name}',
                '-keyout', f'{name}.key', '-out', f'{name}.csr')
        openssl('x509', '-req', '-in', f'{name}.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
                '-CAcreateserial', '-days', '7', '-extfile', f'{name}.ext', '-out', f'{name}.pem')
    for path in root.iterdir():
        if path.is_file():
            path.chmod(0o600)


def secret_name(name):
    return f'{POD}-{name.replace(".", "-")}'


def start(gateway=False, authorization_test=False):
    if not (STATE/'credentials.json').exists():
        configure(STATE, authorization_test=authorization_test)
    if authorization_test and 'authorization_test' not in json.loads((STATE/'credentials.json').read_text()):
        raise RuntimeError('Authorization test needs a new named fixture; existing realm is not modified')
    if subprocess.run(['podman', 'pod', 'exists', POD], stdout=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError('Lab pod already exists; use check, or down before restarting')
    lock = STATE/'images.json'
    images = dict(IMAGES)
    for component, image in IMAGES.items():
        print(f'Acquire {component}: {images.get(component, image)}', flush=True)
        if subprocess.run(['podman', 'image', 'exists', images.get(component, image)]).returncode != 0:
            command(['podman', 'pull', images.get(component, image)], stdout=subprocess.DEVNULL)
        details = json.loads(subprocess.check_output(['podman', 'image', 'inspect', images.get(component, image)]))[0]
        images[component] = details['RepoDigests'][0]
    write(STATE, 'images.json', images)
    command(pod_args(gateway), stdout=subprocess.DEVNULL)
    # Secrets are mounted only into their consuming containers. The CA private
    # key and browser test passwords never enter the pod (realm import excepted).
    files = ['ca.pem', 'server.pem', 'server.key', 'realm.json', 'db-password',
             'keycloak.conf', 'oauth.cfg', 'perses.yaml', 'prometheus.yaml',
             'loki.yaml', 'tempo.yaml', 'collector.yaml', *PERSES_ASSETS]
    for name in files:
        with (STATE/name).open('rb') as stream:
            command(['podman', 'secret', 'create', '--label', LABEL, secret_name(name), '-'],
                    stdin=stream, stdout=subprocess.DEVNULL)

    def run(component, mounts, args=(), extra=(), memory='512m'):
        cmd = ['podman', 'run', '--detach', '--pod', POD, '--name', f'{POD}-{component}',
               '--label', LABEL, '--memory', memory, '--pids-limit', '256',
               '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL']
        for source, target in mounts:
            cmd += ['--secret', f'{secret_name(source)},target={target},mode=0444']
        command([*cmd, *extra, images[component], *args], stdout=subprocess.DEVNULL)

    run('db', [('db-password', '/run/secrets/db-password')],
        extra=['--user', '999:999', '-e', 'POSTGRES_USER=keycloak', '-e', 'POSTGRES_DB=keycloak',
               '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/db-password',
               '-v', f'{POD}-db:/var/lib/postgresql/data:U'])
    for _ in range(60):
        if subprocess.run(['podman', 'exec', f'{POD}-db', 'pg_isready', '-U', 'keycloak'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Keycloak database did not become ready')
    tls = [(name, f'/lab/{name}') for name in ('ca.pem', 'server.pem', 'server.key')]
    run('keycloak', tls+[('keycloak.conf', '/lab/keycloak.conf'),
                        ('realm.json', '/opt/keycloak/data/import/realm.json')],
        ['--config-file=/lab/keycloak.conf', 'start', '--import-realm'], memory='1536m')
    wait_url('https://localhost:18443/realms/enmaas-lab/.well-known/openid-configuration')
    run('perses', [('perses.yaml', '/lab/perses.yaml')]+[(name, f'/etc/perses/lab/{name}')
        for name in PERSES_ASSETS], ['--config=/lab/perses.yaml'],
        extra=['-v', f'{POD}-perses:/perses:U'])
    run('auth', tls+[('oauth.cfg', '/lab/oauth.cfg')], ['--config=/lab/oauth.cfg'], memory='128m')
    run('prometheus', [('prometheus.yaml', '/lab/prometheus.yaml')],
        ['--config.file=/lab/prometheus.yaml', '--storage.tsdb.path=/prometheus',
         '--storage.tsdb.retention.time=48h', '--storage.tsdb.retention.size=512MB'],
        extra=['-v', f'{POD}-prometheus:/prometheus:U'])
    run('loki', [('loki.yaml', '/lab/loki.yaml')], ['-config.file=/lab/loki.yaml'],
        extra=['-v', f'{POD}-loki:/loki:U'])
    run('tempo', [('tempo.yaml', '/lab/tempo.yaml')], ['-config.file=/lab/tempo.yaml'],
        extra=['-v', f'{POD}-tempo:/var/tempo:U'])
    run('collector', tls+[('collector.yaml', '/lab/collector.yaml')], ['--config=/lab/collector.yaml'], memory='256m')
    check()


def context(client=False):
    ctx = ssl.create_default_context(cafile=str(STATE/'ca.pem'))
    if client:
        ctx.load_cert_chain(STATE/'client.pem', STATE/'client.key')
    return ctx


def wait_url(url):
    for _ in range(120):
        try:
            with urllib.request.urlopen(url, context=context(), timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError(f'HTTPS readiness failed: {url}; inspect container status/logs locally')


def require_tls_rejection(request, tls_context):
    """A network outage or HTTP denial is not evidence of client-certificate TLS."""
    try:
        response = urllib.request.urlopen(request, context=tls_context, timeout=5)
    except ssl.SSLError:
        return
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            return
        raise RuntimeError('Client-certificate probe did not establish TLS rejection') from None
    except OSError:
        raise RuntimeError('Client-certificate probe failed before TLS qualification') from None
    response.close()
    raise RuntimeError('Collector accepted TLS without a client certificate')


def check():
    for component, endpoint in (('perses', '8080/api/v1/health'),
                                ('prometheus', '9090/-/ready'),
                                ('loki', '3100/ready'), ('tempo', '3200/ready')):
        for _ in range(60):
            result = subprocess.run(['podman', 'exec', f'{POD}-prometheus', 'wget', '-qO-',
                                     f'http://127.0.0.1:{endpoint}'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f'{component} did not become healthy')
    wait_url('https://localhost:18443/realms/enmaas-lab/.well-known/openid-configuration')
    wait_url('https://localhost:19443/ping')
    try:
        urllib.request.urlopen('https://localhost:19443/oauth2/auth', context=context(), timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, f'Expected anonymous 401, got {exc.code}'
    else:
        raise RuntimeError('Anonymous observability access was accepted')
    payload = json.dumps({'resourceMetrics': []}).encode()
    request = urllib.request.Request('https://localhost:24443/v1/metrics', data=payload,
                                     headers={'Content-Type': 'application/json'})
    require_tls_rejection(request, context())
    with urllib.request.urlopen(request, context=context(client=True), timeout=5) as response:
        assert response.status == 200
    print('PASS: verified issuer TLS; anonymous UI denial; collector requires client certificate.')
    print('Browser: https://localhost:19443 — private CA:', STATE/'ca.pem')
    print('Test credentials (not printed):', STATE/'credentials.json')
    print('This is transport coverage; run browser and signal tests separately.')


def test():
    check()
    command(['python3', str(HERE/'collector-trust.py')])
    command(['python3', str(HERE/'signals.py')])
    env = dict(os.environ)
    env['NODE_EXTRA_CA_CERTS'] = str(STATE/'ca.pem')
    env.setdefault('NODE_PATH', str(ROOT/'tests/pricetag/browser/node_modules'))
    command(['node', str(HERE/'browser.cjs'), str(STATE)], env=env)
    command(['node', str(HERE/'oidc-client.cjs'), str(STATE)], env=env)


def down():
    # Fixed dedicated names; never use system prune or delete unrelated resources.
    if subprocess.run(['podman', 'pod', 'exists', POD]).returncode == 0:
        info = json.loads(subprocess.check_output(['podman', 'pod', 'inspect', POD]))[0]
        if info.get('Labels', {}).get('io.enmaas.test-lab') != INSTANCE:
            raise RuntimeError('Refusing to remove a pod without our ownership label')
        command(['podman', 'pod', 'rm', '--force', POD])
    result = subprocess.check_output(['podman', 'secret', 'ls', '--format', '{{.Name}}'], text=True)
    for name in result.splitlines():
        if not name.startswith(POD+'-'):
            continue
        metadata = json.loads(subprocess.check_output(['podman', 'secret', 'inspect', name]))[0]
        if metadata.get('Spec', {}).get('Labels', {}).get('io.enmaas.test-lab') != INSTANCE:
            raise RuntimeError('Refusing to remove a secret without our ownership label')
        command(['podman', 'secret', 'rm', name], stdout=subprocess.DEVNULL)
    print('Containers/secrets removed; named data volumes and private local state retained.')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'check', 'test', 'down'))
    parser.add_argument('--gateway', action='store_true', help='reserve loopback TLS port for the real gateway SSO test')
    parser.add_argument('--authorization-test', action='store_true', help='add a realm-management client only to a fresh disposable local realm')
    args = parser.parse_args()
    try:
        {'start': lambda: start(args.gateway, args.authorization_test), 'check': check, 'test': test, 'down': down}[args.action]()
    except (subprocess.CalledProcessError, RuntimeError, FileExistsError) as error:
        raise SystemExit(str(error)) from error
