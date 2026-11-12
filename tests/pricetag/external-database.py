#!/usr/bin/env python3
"""Validate external-database URL handling against a disposable PostgreSQL server."""
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
from prepare import POSTGRES, external_client_environment, external_password_file

PODMAN = os.environ.get('PODMAN', 'podman').split()


def run(command, capture=False):
    result = subprocess.run([str(item) for item in command], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def wait_for_postgres(container):
    for _ in range(60):
        result = subprocess.run(PODMAN + ['exec', container, 'pg_isready', '-U', 'postgres'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(1)
    logs = subprocess.run(PODMAN + ['logs', container], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
    raise RuntimeError('disposable PostgreSQL did not become ready:\n' + logs[-4000:])


def main():
    identifier = 'pricetag-external-db-' + uuid.uuid4().hex[:10]
    network = identifier + '-network'
    container = identifier + '-server'
    password = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory() as directory:
        certificates = Path(directory)
        certificate = certificates / 'server.crt'
        key = certificates / 'server.key'
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-subj', '/CN=external-db.test', '-addext', 'subjectAltName=DNS:external-db.test',
             '-keyout', key, '-out', certificate])
        try:
            run(PODMAN + ['network', 'create', network])
            run(PODMAN + ['run', '-d', '--name', container, '--network', network,
                 '--network-alias', 'external-db.test', '--network-alias', 'wrong-db.test', '--env', 'POSTGRES_PASSWORD=' + password,
                 POSTGRES])
            wait_for_postgres(container)
            for source, target in ((certificate, '/tmp/server.crt'), (key, '/tmp/server.key')):
                with source.open('rb') as stream:
                    subprocess.run(PODMAN + ['exec', '--user', '999', '-i', container,
                                   'sh', '-c', f'cat > {target}'],
                                   stdin=stream, check=True)
            run(PODMAN + ['exec', '--user', '999', container, 'chmod', '0600', '/tmp/server.key'])
            run(PODMAN + ['exec', container, 'psql', '-U', 'postgres', '-d', 'postgres',
                 '-c', "ALTER SYSTEM SET ssl = 'on';"])
            run(PODMAN + ['exec', container, 'psql', '-U', 'postgres', '-d', 'postgres',
                 '-c', "ALTER SYSTEM SET ssl_cert_file = '/tmp/server.crt';"])
            run(PODMAN + ['exec', container, 'psql', '-U', 'postgres', '-d', 'postgres',
                 '-c', "ALTER SYSTEM SET ssl_key_file = '/tmp/server.key';"])
            run(PODMAN + ['restart', container])
            wait_for_postgres(container)
            for role in ('maas', 'metering'):
                role_password = secrets.token_urlsafe(24)
                run(PODMAN + ['exec', container, 'psql', '-U', 'postgres', '-d', 'postgres',
                     '-c', f"CREATE ROLE {role} LOGIN PASSWORD '{role_password}';"])
                run(PODMAN + ['exec', container, 'psql', '-U', 'postgres', '-d', 'postgres',
                     '-c', f'CREATE DATABASE {role} OWNER {role};'])
                url = (f'postgresql://{role}:{quote(role_password, safe="")}@external-db.test:5432/{role}'
                       '?sslmode=verify-full')
                environment = external_client_environment(url, role, True)
                environment_file = certificates / f'{role}.env'
                environment_file.write_text(''.join(f'{name}={value}\n'
                                                     for name, value in environment.items()))
                password_file = certificates / f'{role}.password'
                password_file.write_text(external_password_file(role_password))
                shell_command = (
                    '. /run/secrets/external-db-password\n'
                    'exec psql -X -qAt -v ON_ERROR_STOP=1 -c '
                    + shlex.quote("SELECT current_user || ':' || current_database()"))
                result = subprocess.run(
                    PODMAN + ['run', '--rm', '--network', network, '--env-file', environment_file,
                     '-v', f'{certificate}:/etc/pricetag-external-ca.crt:ro,Z',
                     '-v', f'{password_file}:/run/secrets/external-db-password:ro,Z',
                     POSTGRES, 'sh', '-c', shell_command],
                    check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
                if result != f'{role}:{role}':
                    raise RuntimeError(f'unexpected connection identity: {result}')
                # A reachable server with a different hostname must fail before authentication.
                wrong_environment = dict(environment, PGHOST='wrong-db.test')
                wrong_file = certificates / f'{role}-wrong-host.env'
                wrong_file.write_text(''.join(f'{name}={value}\n' for name, value in wrong_environment.items()))
                rejected = subprocess.run(
                    PODMAN + ['run', '--rm', '--network', network, '--env-file', wrong_file,
                     '-v', f'{certificate}:/etc/pricetag-external-ca.crt:ro,Z',
                     '-v', f'{password_file}:/run/secrets/external-db-password:ro,Z',
                     POSTGRES, 'sh', '-c', shell_command],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if rejected.returncode == 0 or 'does not match host name' not in rejected.stderr:
                    raise RuntimeError('expected certificate hostname rejection was not observed')
            print('PASS: external MaaS and metering connections verify TLS; wrong server hostname rejected')
        finally:
            subprocess.run(PODMAN + ['rm', '-f', container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(PODMAN + ['network', 'rm', network],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
