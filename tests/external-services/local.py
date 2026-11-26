#!/usr/bin/env python3
"""Disposable loopback-only SSO/telemetry fixture; no AWS or host trust changes."""
import argparse
import importlib.util
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
_monitor_spec=importlib.util.spec_from_file_location('lab_monitoring',HERE/'benchmark_monitoring.py')
monitoring=importlib.util.module_from_spec(_monitor_spec);_monitor_spec.loader.exec_module(monitoring)
ROOT = HERE.parents[1]
INSTANCE = os.environ.get('ENMAAS_LAB_INSTANCE', 'external-local')
if not re.fullmatch(r'[a-z][a-z0-9-]{2,31}', INSTANCE):
    raise ValueError('Invalid local lab instance name')
STATE = ROOT/'.state'/('external-services-local' if INSTANCE == 'external-local' else INSTANCE)
POD = 'enmaas-' + INSTANCE
LABEL = 'io.enmaas.test-lab=' + INSTANCE
_offset = os.environ.get('ENMAAS_LAB_PORT_OFFSET', '0')
if not re.fullmatch(r'[0-9]+', _offset) or int(_offset) > 30000:
    raise ValueError('Invalid local lab port offset')
OFFSET = int(_offset)
DEFAULT_PORTS = {'issuer': 18443, 'observability': 19443, 'collector': 24443, 'gateway': 28443}


def port(role):
    return DEFAULT_PORTS[role] + OFFSET


def url(role, path=''):
    return f'https://localhost:{port(role)}{path}'


PORTS = tuple(port(role) for role in ('issuer', 'observability', 'collector'))
IMAGES = {name: entry['image'] for name, entry in json.loads((HERE/'images.json').read_text()).items()}
_service_spec=importlib.util.spec_from_file_location('local_service_config',HERE/'service_config.py')
service_config=importlib.util.module_from_spec(_service_spec);_service_spec.loader.exec_module(service_config)
PERSES_ASSETS=(*service_config.PERSES_ASSETS, 'benchmark-dashboard.json')
privacy_transform=service_config.privacy_transform


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def pod_args(gateway=False):
    args = ['podman', 'pod', 'create', '--name', POD, '--label', LABEL]
    for number in (*PORTS, *((port('gateway'),) if gateway else ())):
        args += ['--publish', f'127.0.0.1:{number}:{number}']
    return args


def write(root, name, value):
    path = root/name
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2)+'\n')
    path.chmod(0o600)


def configure(root, certificates=True, authorization_test=False):
    service_config.realm.configure(root,authorization_test=authorization_test)
    credentials=json.loads((root/'credentials.json').read_text())
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
    service_config.configure(root,credentials['oidc'])
    monitoring.write(root)  # Benchmark assets belong only to the disposable local lab.
    # Render only the known listener ports in this fixture's new configuration.
    # One regex pass prevents a shifted port being shifted again.
    pattern = r'(?<=:)(18443|19443|24443|28443)(?![0-9])'
    for name in ('realm.json', 'gateway-oidc.json', 'keycloak.conf', 'oauth.cfg', 'collector.yaml'):
        path = root/name
        text = re.sub(pattern, lambda match: str(int(match[0]) + OFFSET), path.read_text())
        if name == 'keycloak.conf':
            text = text.replace('https-port=18443', f"https-port={port('issuer')}")
        write(root, name, text)
    write(root, 'endpoints.json', {'offset': OFFSET, 'urls': {role: url(role) for role in DEFAULT_PORTS}})
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


def check_database_shm():
    # Podman can inherit the pod's /dev/shm even when --shm-size is supplied.
    # Verify the mounted filesystem rather than trusting launch arguments.
    result=subprocess.check_output(['podman','exec',f'{POD}-db','df','-B1','/dev/shm'],text=True)
    try:size=int(result.splitlines()[1].split()[1])
    except (IndexError,ValueError):raise RuntimeError('Cannot verify PostgreSQL shared-memory mount') from None
    if size!=512*1024*1024:raise RuntimeError('PostgreSQL requires its private 512 MiB shared-memory mount')


def check_endpoints():
    path = STATE/'endpoints.json'
    if not path.exists():
        if OFFSET and (STATE/'credentials.json').exists():
            raise ValueError('Existing fixture has no port offset metadata')
        return
    expected = {'offset': OFFSET, 'urls': {role: url(role) for role in DEFAULT_PORTS}}
    if json.loads(path.read_text()) != expected:
        raise ValueError('Selected port offset differs from the existing fixture')


def start(gateway=False, authorization_test=False):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_endpoints()
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
             'keycloak.conf', 'oauth.cfg', 'perses.yaml', 'prometheus.yaml', 'monitoring-rules.yaml',
             'loki.yaml', 'tempo.yaml', 'collector.yaml', *PERSES_ASSETS]
    for name in files:
        # input= creates a pipe, as required by native Podman 4 for '-'.
        command(['podman', 'secret', 'create', '--label', LABEL, secret_name(name), '-'],
                input=(STATE/name).read_bytes(), stdout=subprocess.DEVNULL)

    def run(component, mounts, args=(), extra=(), memory='512m'):
        cmd = ['podman', 'run', '--detach', '--pod', POD, '--name', f'{POD}-{component}',
               '--label', LABEL, '--memory', memory, '--pids-limit', '256',
               '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL']
        for source, target in mounts:
            cmd += ['--secret', f'{secret_name(source)},target={target},mode=0444']
        command([*cmd, *extra, images[component], *args], stdout=subprocess.DEVNULL)

    run('db', [('db-password', '/run/secrets/db-password')],
        memory='2g',
        extra=['--ipc', 'private', '--tmpfs', '/dev/shm:rw,nosuid,nodev,noexec,size=512m,mode=1777', '--user', '999:999', '-e', 'POSTGRES_USER=keycloak', '-e', 'POSTGRES_DB=keycloak',
               '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/db-password',
               '-v', f'{POD}-db:/var/lib/postgresql/data:U'])
    for _ in range(60):
        if subprocess.run(['podman', 'exec', f'{POD}-db', 'pg_isready', '-U', 'keycloak'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Keycloak database did not become ready')
    check_database_shm()
    tls = [(name, f'/lab/{name}') for name in ('ca.pem', 'server.pem', 'server.key')]
    run('keycloak', tls+[('keycloak.conf', '/lab/keycloak.conf'),
                        ('realm.json', '/opt/keycloak/data/import/realm.json')],
        ['--config-file=/lab/keycloak.conf', 'start', '--import-realm'], memory='1536m')
    wait_url(url('issuer', '/realms/enmaas-lab/.well-known/openid-configuration'))
    run('perses', [('perses.yaml', '/lab/perses.yaml')]+[(name, f'/etc/perses/lab/{name}')
        for name in PERSES_ASSETS], ['--config=/lab/perses.yaml'],
        extra=['-v', f'{POD}-perses:/perses:U'])
    run('auth', tls+[('oauth.cfg', '/lab/oauth.cfg')], ['--config=/lab/oauth.cfg'], memory='128m')
    run('prometheus', [('prometheus.yaml', '/lab/prometheus.yaml'),('monitoring-rules.yaml','/lab/monitoring-rules.yaml')],
        ['--config.file=/lab/prometheus.yaml', '--storage.tsdb.path=/prometheus',
         '--storage.tsdb.retention.time=48h', '--storage.tsdb.retention.size=512MB'],
        extra=['-v', f'{POD}-prometheus:/prometheus:U'])
    run('loki', [('loki.yaml', '/lab/loki.yaml')], ['-config.file=/lab/loki.yaml'],
        extra=['-v', f'{POD}-loki:/loki:U'])
    run('tempo', [('tempo.yaml', '/lab/tempo.yaml')], ['-config.file=/lab/tempo.yaml'],
        extra=['-v', f'{POD}-tempo:/var/tempo:U','--env','GOMEMLIMIT=768MiB'], memory='1g')
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


def check_port_bindings():
    """Inspect actual pod exposure before declaring the local fixture healthy."""
    check_endpoints()
    info = json.loads(subprocess.check_output(['podman', 'pod', 'inspect', POD], timeout=15))[0]
    if info.get('Labels', {}).get('io.enmaas.test-lab') != INSTANCE:
        raise RuntimeError('Local pod ownership does not match the selected fixture')
    bindings = info.get('InfraConfig', {}).get('PortBindings')
    required = {str(port)+'/tcp' for port in PORTS}
    if not isinstance(bindings, dict) or set(bindings) not in (required, required | {str(port('gateway'))+'/tcp'}):
        raise RuntimeError('Unexpected local pod port mappings')
    for endpoint, entries in bindings.items():
        expected = [{'HostIp':'127.0.0.1', 'HostPort':endpoint.split('/')[0]}]
        if entries != expected:
            raise RuntimeError('Local pod ports must use exact loopback-only bindings')


def check():
    check_port_bindings()
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
    wait_url(url('issuer', '/realms/enmaas-lab/.well-known/openid-configuration'))
    wait_url(url('observability', '/ping'))
    try:
        urllib.request.urlopen(url('observability', '/oauth2/auth'), context=context(), timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, f'Expected anonymous 401, got {exc.code}'
    else:
        raise RuntimeError('Anonymous observability access was accepted')
    payload = json.dumps({'resourceMetrics': []}).encode()
    request = urllib.request.Request(url('collector', '/v1/metrics'), data=payload,
                                     headers={'Content-Type': 'application/json'})
    require_tls_rejection(request, context())
    with urllib.request.urlopen(request, context=context(client=True), timeout=5) as response:
        assert response.status == 200
    print('PASS: loopback-only pod ports; verified issuer TLS; anonymous UI denial; collector requires client certificate.')
    print('Browser: ' + url('observability') + ' — private CA:', STATE/'ca.pem')
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
    except (subprocess.CalledProcessError, RuntimeError, ValueError, FileExistsError) as error:
        raise SystemExit(str(error)) from error
