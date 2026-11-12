#!/usr/bin/env python3
"""Run isolated identity and telemetry security checks without deployment credentials."""
import json
from pathlib import Path
import subprocess
import sys
import signal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'scripts/pricetag'))
import prepare


def run_check(script, arguments, timeout=600, cleanup_timeout=90):
    # SIGTERM must unwind the fixture's finally blocks before escalation. A
    # subprocess.run timeout would immediately kill Python, skipping cleanup.
    bootstrap = '''import runpy,signal,sys
def stop(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    process = subprocess.Popen([sys.executable, '-c', bootstrap, str(script), *arguments],
                               start_new_session=True)
    try:
        status = process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.terminate()
        try:
            process.wait(timeout=cleanup_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            print('Cleanup did not finish; inspect disposable test resources before reuse.',
                  file=sys.stderr)
        raise
    if status:
        raise subprocess.CalledProcessError(status, [str(script)])


def main():
    images = json.loads((HERE / 'images.json').read_text())
    for name, image in [('python', prepare.PYTHON), *(
            (name, images[name]['image']) for name in ('keycloak', 'auth', 'collector'))]:
        print(f'Acquire pinned {name} image', flush=True)
        subprocess.run(['podman', 'pull', image], check=True, timeout=300)
    for script, arguments in (
            ('private-issuer-runtime.py', ['--keycloak']),
            ('issuer-lockout.py', []),
            ('redaction.py', [])):
        print(f'Run {script}', flush=True)
        run_check(HERE / script, arguments)
    print('PASS: isolated identity and collector security checks', flush=True)


if __name__ == '__main__':
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    main()
