#!/usr/bin/env python3
"""Verify pulls and provenance of the installer's released image set."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def verify_metadata(metadata, revision=None):
    if metadata.get('Architecture') != 'amd64' or metadata.get('Os') != 'linux':
        raise ValueError('released images must support linux/amd64')
    labels = metadata.get('Labels') or metadata.get('Config', {}).get('Labels') or {}
    if revision and labels.get('org.opencontainers.image.revision') != revision:
        raise ValueError('image revision does not match the installer source pin')


def pull_and_inspect(engine, image, revision, env, pull):
    subprocess.run([*pull, image], check=True, env=env)
    metadata = json.loads(subprocess.check_output(
        [engine, 'image', 'inspect', image], env=env, text=True))[0]
    verify_metadata(metadata, revision)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('docker', 'podman'), default='docker')
    args = parser.parse_args()
    # Load deployment dependencies only in the standalone CLI process. Unit
    # discovery also imports a test fixture named gateway in its own process.
    spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/pricetag/install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
    from prepare import POSTGRES

    # Every installer image now lives in a public repository, so anonymous
    # pull evidence covers the complete released set, Praxis included.
    images = (
        ('Praxis', installer.PRAXIS_IMAGE, installer.PRAXIS_REVISION),
        ('MaaS', installer.PUBLISHED_MAAS_IMAGE, installer.MAAS_COMMIT),
        ('PriceTag', installer.PUBLISHED_METERING_IMAGE, installer.METERING_COMMIT),
        ('PostgreSQL', POSTGRES, None),
    )
    # Never use workstation or CI registry credentials for public-pull evidence.
    with tempfile.TemporaryDirectory(prefix='public-image-auth-') as directory:
        authfile = Path(directory) / 'config.json'
        authfile.write_text('{"auths": {}}\n')
        authfile.chmod(0o600)
        env = {**os.environ, 'DOCKER_CONFIG': directory, 'REGISTRY_AUTH_FILE': str(authfile)}
        for name, image, revision in images:
            print(f'Checking public {name} image: {image}', flush=True)
            pull = [args.engine, 'pull', '--platform', 'linux/amd64']
            if args.engine == 'podman':
                pull += ['--authfile', str(authfile)]
            pull_and_inspect(args.engine, image, revision, env, pull)
    print('PASS: all public installer images are anonymous-pullable linux/amd64 images with expected application revisions')


if __name__ == '__main__':
    main()
