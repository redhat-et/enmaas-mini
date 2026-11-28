#!/usr/bin/env python3
"""Smoke-test the two application images against disposable PostgreSQL."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/pricetag'))
from prepare import POSTGRES


def podman(*args, **kwargs):
    return subprocess.run(['podman', *map(str, args)], check=True, text=True,
                          stdout=subprocess.PIPE, **kwargs).stdout.strip()


def status(url, data=None):
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def ready(url):
    for _ in range(90):
        try:
            if status(url) == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('Service did not become ready: ' + url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--maas-image', required=True)
    parser.add_argument('--metering-image', required=True)
    args = parser.parse_args()
    prefix = 'pricetag-ci-' + uuid.uuid4().hex[:12]
    containers = [prefix + '-' + role for role in ('db', 'maas', 'metering')]
    db, maas, metering = containers
    podman('network', 'create', prefix)
    try:
        network = json.loads(podman('network', 'inspect', prefix))[0]
        with tempfile.TemporaryDirectory(prefix='pricetag-image-test-') as directory:
            root = Path(directory)
            # Synthetic credentials only; nothing is published beyond loopback.
            password = secrets.token_hex(24)
            pg_env = root / 'postgres.env'
            pg_env.write_text('POSTGRES_PASSWORD=' + password + '\n')
            pg_env.chmod(0o600)
            # The image account creates its own 0700 PGDATA below a writable
            # tmpfs (root-owned on podman 5), so no chown of the mount is
            # needed; 1 GiB holds bootstrap databases plus WAL.
            podman('run', '-d', '--name', db, '--network', prefix, '--network-alias', 'db',
                   '--env-file', pg_env, '--tmpfs', '/var/lib/postgresql/data:rw,size=1g,mode=1777',
                   '-e', 'PGDATA=/var/lib/postgresql/data/pgdata', POSTGRES)
            for _ in range(90):
                if subprocess.run(['podman', 'exec', db, 'pg_isready', '-U', 'postgres'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('PostgreSQL did not become ready')
            for database in ('maas', 'metering'):
                podman('exec', db, 'createdb', '-U', 'postgres', database)
            maas_env = root / 'maas.env'
            # Synthetic credentials only; the env file is read client-side by
            # podman, so it never needs to be readable inside a container.
            maas_env.write_text(f'DATABASE_URL=postgres://postgres:{password}@db:5432/maas?sslmode=disable\n'
                                'TENANT=models-as-a-service\nAPI_KEY_MAX_EXPIRATION_DAYS=90\n'
                                'METRICS_ENABLED=false\nPORT=8080\n')
            maas_env.chmod(0o600)
            common = ['--network', prefix, '--read-only', '--cap-drop', 'all',
                      '--security-opt', 'no-new-privileges', '--user', '1001:1001',
                      '-p', '127.0.0.1::8080']
            # enmaas-api creates no tables at runtime: run its idempotent
            # migration before the API, exactly as scripts/pricetag/start does.
            # The hardened read-only rootfs is sufficient: app.migrate only
            # executes DDL over the database connection; PYTHONDONTWRITEBYTECODE=1
            # makes that no-writes contract explicit instead of relying on
            # CPython silently skipping __pycache__ writes it cannot make on
            # the read-only rootfs, and no writable mount (not even /tmp) is
            # provided deliberately — the API container below runs with the
            # same read-only rootfs and needs none either, so a migration
            # that suddenly required one would fail loudly here instead of
            # passing unnoticed. DATABASE_URL's `db` host
            # resolves through the database container's --network-alias db
            # above; the one-shot needs no alias of its own because name
            # resolution is network-wide. The image starts the API via CMD
            # (no ENTRYPOINT), so the migration is a plain command override
            # from the pinned /srv/enmaas-api workdir, and maas_env is the
            # image's complete runtime contract — the MAAS_RUNTIME_MODE-style
            # flags previously passed here belonged to the retired Ladas
            # image. The plain-HTTP /health readiness below asserts the
            # standalone binding. Unlike scripts/pricetag/start, no fixed
            # address is needed here: this run creates a fresh throwaway
            # network, every container on it allocates dynamically, and the
            # one-shot completes before the API container starts, so there
            # is no static service address it could collide with.
            podman('run', '--rm', '--network', prefix, '--read-only', '--cap-drop', 'all',
                   '--security-opt', 'no-new-privileges', '--user', '1001:1001',
                   '--workdir', '/srv/enmaas-api', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                   '--env-file', maas_env, args.maas_image, 'python', '-m', 'app.migrate')
            podman('run', '-d', '--name', maas, *common, '--network-alias', 'maas',
                   '--env-file', maas_env, args.maas_image)
            maas_url = 'http://' + podman('port', maas, '8080/tcp')
            ready(maas_url + '/health')
            env = root / 'metering.env'
            env.write_text(f'DATABASE_URL=postgres://postgres:{password}@db:5432/metering?sslmode=disable\n'
                           'M2M_AUTH_REQUIRED=true\nM2M_SHARED_SECRET=' + secrets.token_hex(32) + '\n'
                           'SESSION_SECRET=' + secrets.token_hex(32) + '\n'
                           'MAAS_VALIDATE_URL=http://maas:8080/internal/v1/api-keys/validate\n')
            env.chmod(0o600)
            podman('run', '-d', '--name', metering, *common, '--env-file', env, args.metering_image)
            metering_url = 'http://' + podman('port', metering, '8080/tcp')
            ready(metering_url + '/ready')
            if status(metering_url + '/api/v1/events', b'{}') != 401:
                raise RuntimeError('Metering accepted a request without machine credentials')
            # Metering migrates itself at startup; the MaaS migration ran
            # explicitly above, without Kubernetes.
            for database in ('maas', 'metering'):
                count = podman('exec', db, 'psql', '-U', 'postgres', '-d', database, '-Atc',
                               "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
                if int(count) == 0:
                    raise RuntimeError(database + ' migrations created no tables')
            print('PASS: standalone readiness, database migrations and metering authentication')
    except Exception:
        for container in containers:
            subprocess.run(['podman', 'logs', '--tail', '30', container], check=False)
        raise
    finally:
        for container in reversed(containers):
            subprocess.run(['podman', 'rm', '-f', container], check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        subprocess.run(['podman', 'network', 'rm', prefix], check=False, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
