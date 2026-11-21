#!/usr/bin/env python3
"""Fresh local stack qualification; no AWS, RHEL or real provider access."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load_lab():
    spec = importlib.util.spec_from_file_location('qualified_local', HERE/'local.py')
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    return lab


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metering-image', default='localhost/pricetag-metering:upstream-2281e1a')
    parser.add_argument('--maas-image', default='localhost/pricetag-maas:standalone')
    args = parser.parse_args()
    if os.environ.get('ENMAAS_LAB_INSTANCE') != 'external-ci':
        parser.error('Set ENMAAS_LAB_INSTANCE=external-ci; other fixtures are never reset')
    lab = load_lab()
    if lab.STATE.exists():
        parser.error('Requires fresh local state; preserve or remove previous evidence manually')
    existing = subprocess.run(['podman', 'pod', 'exists', lab.POD],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if existing.returncode != 1:
        parser.error('Cannot establish a fresh pod; existing pods are never reset')
    env = dict(os.environ)
    env.setdefault('NODE_PATH', str(ROOT/'tests/pricetag/browser/node_modules'))
    env['NODE_EXTRA_CA_CERTS'] = str(lab.STATE/'ca.pem')
    completed = []

    def run(name, *arguments, timeout=240):
        print('Qualify: ' + name, flush=True)
        interpreter = 'node' if name.endswith('.cjs') else sys.executable
        subprocess.run([interpreter, str(HERE/name), *map(str, arguments)],
                       cwd=ROOT, env=env, check=True, timeout=timeout)
        completed.append(name + (':' + str(arguments[0]) if name in ('local.py', 'gateway-sso.py') else ''))

    try:
        run('local.py', 'start', '--gateway', '--authorization-test', timeout=480)
        run('collector-trust.py')
        run('signals.py')
        run('browser.cjs', lab.STATE)
        run('gateway-sso.py', 'start', '--praxis-image', lab.IMAGES['praxis'],
            '--metering-image', args.metering_image, '--maas-image', args.maas_image)
        run('gateway-sso.py', 'test')
        run('gateway-sso.py', 'inference')
        run('persona-runtime.py')
        run('population-runtime.py')
        run('group-revocation.cjs', lab.STATE)
        run('gateway-lifecycle.cjs', lab.STATE)
        run('gateway-signals.py', '--praxis-image', lab.IMAGES['praxis'])
        run('metric-freshness.py')
        run('log-runtime.py')
        run('local-host-metrics.py', '--samples', '2', '--interval', '1')
    finally:
        # Cleanup verifies ownership; never prune images, other pods or volumes.
        run('local.py', 'down', timeout=120)
    lab.write(lab.STATE, 'qualification.json', {'passed': True, 'checks': completed,
        'not_qualified': ['RHEL', 'AWS', 'public access', 'sustained load', 'capacity sizing']})
    print('PASS: local transport, identities, accounting and telemetry; evidence retained privately')


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit('Local qualification failed; inspect the selected fixture and private evidence') from None
