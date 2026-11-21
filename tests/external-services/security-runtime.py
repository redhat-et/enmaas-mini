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


def run_check(script, arguments, timeout=600, cleanup_timeout=90, *, env=None, cwd=None):
    # SIGTERM must unwind the fixture's finally blocks before escalation. A
    # subprocess.run timeout would immediately kill Python, skipping cleanup.
    bootstrap = '''import runpy,signal,sys
from pathlib import Path
def stop(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stop)
sys.argv=sys.argv[1:]
sys.path.insert(0,str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    process = subprocess.Popen([sys.executable, '-c', bootstrap, str(script), *arguments],
                               start_new_session=True, env=env, cwd=cwd)
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
    run_check(HERE / 'image-platforms.py', [], timeout=600)
    images = json.loads((HERE / 'images.json').read_text())
    for name, image in [('python', prepare.PYTHON), *(
            (name, images[name]['image']) for name in ('keycloak', 'auth', 'collector', 'praxis'))]:
        print(f'Acquire pinned {name} image', flush=True)
        subprocess.run(['podman', 'pull', image], check=True, timeout=300)
    for script, arguments in (
            ('private-issuer-runtime.py', ['--keycloak', '--provisioning']),
            ('issuer-lockout.py', []),
            ('issuer-routes-runtime.py', []),
            ('transfer-runtime.py', []),
            ('redaction.py', [])):
        print(f'Run {script}', flush=True)
        run_check(HERE / script, arguments)
    print('PASS: isolated identity and collector security checks', flush=True)


if __name__ == '__main__':
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    main()
