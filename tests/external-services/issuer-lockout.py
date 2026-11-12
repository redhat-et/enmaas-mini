#!/usr/bin/env python3
"""Test generated Keycloak lockout in an isolated, loopback-only H2 fixture.

No administrator API, existing realm, cloud access or real credentials are used.
"""
import html.parser
import http.cookiejar
import importlib.util
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

spec = importlib.util.spec_from_file_location('lockout_lab', Path(__file__).with_name('local.py'))
lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)


class Form(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form' and values.get('id') == 'kc-form-login':
            self.action = values.get('action')


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, request, response, code, message, headers, url):
        if not url.startswith(self.origin+'/'):
            return None
        return super().redirect_request(request, response, code, message, headers, url)


def login(origin, context, password):
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), LocalRedirect(origin))
    callback = 'https://localhost:19443/oauth2/callback'
    query = urllib.parse.urlencode({'client_id': 'observability', 'redirect_uri': callback,
        'response_type': 'code', 'scope': 'openid', 'state': secrets.token_hex(16)})
    with opener.open(origin+'/realms/enmaas-lab/protocol/openid-connect/auth?'+query, timeout=10) as response:
        document = response.read(1_000_001)
    if len(document) > 1_000_000:
        raise RuntimeError('unexpected login page size')
    form = Form(); form.feed(document.decode())
    if not form.action or not form.action.startswith(origin+'/'):
        raise RuntimeError('login action does not belong to the isolated issuer')
    request = urllib.request.Request(form.action,
        data=urllib.parse.urlencode({'username': 'admin', 'password': password}).encode())
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read(1_000_001).decode()
            return False, 'Invalid username or password' in body
    except urllib.error.HTTPError as error:
        if error.code == 302:
            target = urllib.parse.urlsplit(error.headers.get('Location', ''))
            expected = urllib.parse.urlsplit(callback)
            if (target.scheme, target.netloc, target.path) != (expected.scheme, expected.netloc, expected.path):
                raise RuntimeError('unexpected callback destination') from None
            return bool(urllib.parse.parse_qs(target.query).get('code')), False
        if error.code in (400, 401):
            return False, 'Invalid username or password' in error.read(1_000_001).decode()
        raise RuntimeError('unexpected issuer login status') from None


def run(*args, **kwargs):
    return subprocess.check_output(['podman', *args], stderr=subprocess.PIPE, **kwargs)


def main():
    name = 'enmaas-lockout-'+secrets.token_hex(5)
    created = []; started = False
    with tempfile.TemporaryDirectory(prefix='enmaas-lockout-') as temporary:
        root = Path(temporary)
        lab.configure(root)
        credentials = json.loads((root/'credentials.json').read_text())
        context = ssl.create_default_context(cafile=str(root/'ca.pem'))
        try:
            mounts = []
            for file, target in (('realm.json', '/opt/keycloak/data/import/realm.json'),
                                 ('server.pem', '/lab/server.pem'), ('server.key', '/lab/server.key')):
                secret = name+'-'+file.replace('.', '-')
                run('secret', 'create', '--label', 'io.enmaas.test-lab=lockout', secret, '-', input=(root/file).read_bytes())
                created.append(secret)
                mounts += ['--secret', secret+',target='+target+',mode=0444']
            run('run', '-d', '--name', name, '--label', 'io.enmaas.test-lab=lockout',
                '--memory', '1536m', '--pids-limit', '256', '--cap-drop', 'all',
                '--security-opt', 'no-new-privileges', '-p', '127.0.0.1::8443',
                *mounts, lab.IMAGES['keycloak'], 'start-dev', '--import-realm',
                '--http-enabled=false', '--https-port=8443', '--hostname=localhost',
                '--https-certificate-file=/lab/server.pem', '--https-certificate-key-file=/lab/server.key')
            started = True
            port = run('port', name, '8443/tcp').decode().strip().rsplit(':', 1)[1]
            origin = 'https://localhost:'+port
            for _ in range(120):
                try:
                    with urllib.request.urlopen(origin+'/realms/enmaas-lab/.well-known/openid-configuration', context=context, timeout=2) as response:
                        if json.load(response)['issuer'] == origin+'/realms/enmaas-lab':break
                except (OSError, ValueError):pass
                time.sleep(1)
            else:raise RuntimeError('isolated issuer not ready')
            if not login(origin, context, credentials['admin'])[0]:
                raise RuntimeError('valid login failed before abuse test')
            print('PASS: valid login before failures', flush=True)
            for _ in range(5):
                accepted, invalid = login(origin, context, 'synthetic-wrong-password')
                if accepted or not invalid:raise RuntimeError('wrong password was not rejected normally')
                time.sleep(1.2)
            if login(origin, context, credentials['admin'])[0]:
                raise RuntimeError('correct password bypassed temporary lockout')
            print('PASS: correct password denied during lockout; waiting for recovery', flush=True)
            for _ in range(9):
                time.sleep(10)
                if login(origin, context, credentials['admin'])[0]:
                    print('PASS: correct password accepted after temporary lockout', flush=True)
                    break
            else:raise RuntimeError('temporary lockout did not recover within 90 seconds')
        finally:
            if started:run('rm', '-f', name)
            for secret in created:run('secret', 'rm', secret)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        # Do not render command arguments, response bodies or credentials.
        raise SystemExit('Isolated issuer lockout test failed ('+type(error).__name__+').') from None
