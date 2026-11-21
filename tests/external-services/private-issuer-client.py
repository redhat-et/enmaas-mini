#!/usr/bin/env python3
"""Disposable fixture client: form login, PKCE exchange and authenticated userinfo.

Run inside private-issuer-runtime.py's client container, never against a real realm.
TLS authenticates the issuer; this is not a replacement for application JWT tests.
"""
import base64
import hashlib
import html.parser
import http.cookiejar
import json
from pathlib import Path
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = 'https://identity.lab.example.com:8443'
ISSUER = ORIGIN + '/realms/enmaas-lab'


class Form(html.parser.HTMLParser):
    action = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form' and values.get('id') == 'kc-form-login':
            self.action = values.get('action')


class IssuerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not url.startswith(ORIGIN + '/'):
            return None
        return super().redirect_request(request, response, code, message, headers, url)


def client():
    context = ssl.create_default_context(cafile='/run/secrets/ca.pem')
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), IssuerRedirect())


def document(opener, request):
    with opener.open(request, timeout=10) as response:
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise RuntimeError('oversized fixture response')
        return body


def authorize(opener, discovery, name, password, client_id, callback, verifier):
    state = secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    query = urllib.parse.urlencode(dict(client_id=client_id, redirect_uri=callback,
        response_type='code', scope='openid profile email', state=state,
        nonce=secrets.token_urlsafe(24), code_challenge=challenge, code_challenge_method='S256'))
    form = Form()
    form.feed(document(opener, discovery['authorization_endpoint'] + '?' + query).decode())
    if not form.action or not form.action.startswith(ORIGIN + '/'):
        raise RuntimeError('unexpected fixture login destination')
    request = urllib.request.Request(form.action, data=urllib.parse.urlencode(
        dict(username=name, password=password)).encode())
    try:
        document(opener, request)
    except urllib.error.HTTPError as error:
        target = urllib.parse.urlsplit(error.headers.get('Location', ''))
        expected = urllib.parse.urlsplit(callback)
        if error.code != 302 or target[:3] != expected[:3]:
            raise RuntimeError('unexpected fixture callback') from None
        values = urllib.parse.parse_qs(target.query)
        if values.get('state') != [state] or len(values.get('code', [])) != 1:
            raise RuntimeError('invalid fixture callback state or code')
        return values['code'][0]
    raise RuntimeError('fixture login did not authorize')


def exchange(opener, endpoint, fields, expected=200):
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode(fields).encode())
    try:
        result = json.loads(document(opener, request))
    except urllib.error.HTTPError as error:
        if error.code != expected:
            raise RuntimeError('unexpected token exchange rejection') from None
        error.close()
        return None
    if expected != 200:
        raise RuntimeError('invalid token exchange accepted')
    return result


def main():
    credentials = json.loads(Path('/run/secrets/credentials.json').read_text())
    identities = json.loads(Path('/run/secrets/identities.json').read_text())
    for client_id, credential, callback in (
        ('enmaas-mini', 'gateway_oidc', 'https://localhost:28443/oauth/callback'),
        ('observability', 'oidc', 'https://localhost:19443/oauth2/callback'),
    ):
        for name in ('admin', 'alice', 'bob'):
            opener = client()
            discovery = json.loads(document(opener, ISSUER + '/.well-known/openid-configuration'))
            if discovery['issuer'] != ISSUER:
                raise RuntimeError('unexpected issuer')
            for field in ('authorization_endpoint', 'token_endpoint', 'userinfo_endpoint'):
                if not discovery[field].startswith(ISSUER + '/'):
                    raise RuntimeError('unexpected protocol destination')
            verifier = secrets.token_urlsafe(48)
            code = authorize(opener, discovery, name, credentials[name], client_id, callback, verifier)
            fields = dict(grant_type='authorization_code', client_id=client_id,
                client_secret=credentials[credential], redirect_uri=callback,
                code=code, code_verifier=verifier)
            tokens = exchange(opener, discovery['token_endpoint'], fields)
            user = json.loads(document(opener, urllib.request.Request(discovery['userinfo_endpoint'],
                headers={'Authorization': 'Bearer ' + tokens['access_token']})))
            if user['sub'] != identities[name]:
                raise RuntimeError('wrong immutable identity')
            if client_id == 'enmaas-mini' and user.get('rhatUUID') != identities[name]:
                raise RuntimeError('missing immutable partner identity')
            if client_id == 'observability' and user.get('groups', []) != (
                ['/observability-admin'] if name == 'admin' else []
            ):
                raise RuntimeError('wrong observability groups')
            exchange(opener, discovery['token_endpoint'], fields, expected=400)
            # A separate code avoids conflating PKCE rejection with replay rejection.
            fresh = client()
            fields['code'] = authorize(fresh, discovery, name, credentials[name], client_id, callback, verifier)
            fields['code_verifier'] = secrets.token_urlsafe(48)
            exchange(fresh, discovery['token_endpoint'], fields, expected=400)
            print(f'PASS: {client_id}/{name} private issuer login, userinfo identity, code replay and wrong PKCE rejection')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No URLs with authorization codes, tokens or credential-bearing bodies.
        raise SystemExit('Private issuer protocol check failed; sensitive responses withheld') from None
