#!/usr/bin/env python3
"""Verify native routing, alias boundaries and accounting on a mock deployment."""
import http.cookiejar
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/pricetag'), str(ROOT / 'tests/common')]
import metering_admin
from contracts import check


class RequestPacer:
    """Keep functional probes below the gateway's 30 requests/s limit."""

    def __init__(self):
        self.previous = None

    def wait(self):
        now = time.monotonic()
        if self.previous is not None:
            remaining = .05 - (now - self.previous)
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        self.previous = now


def invalid_bodies(body, upstream_id, public_aliases):
    """Build negative requests without rejecting a configured public alias."""
    invalid = [{**body, 'model': 'unknown/model'}, {}, b'{"model":']
    if upstream_id not in public_aliases:
        invalid.append({**body, 'model': upstream_id})
    return invalid


def main():
    if os.geteuid() != 0:
        raise ValueError('run as root on the mock test host')
    loader = importlib.machinery.SourceFileLoader('benchmark', str(ROOT / 'scripts/pricetag/benchmark'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    benchmark = importlib.util.module_from_spec(spec)
    loader.exec_module(benchmark)
    deploy = Path('/etc/praxis-pricetag')
    benchmark.require_mock(deploy)
    models = json.loads((deploy / 'models.json').read_text())
    origin = next(line.split('=', 1)[1] for line in (deploy / 'metering.env').read_text().splitlines()
                  if line.startswith('MANAGEMENT_PUBLIC_ORIGIN='))
    tls = ssl.create_default_context(cafile=str(deploy / 'ca.pem'))
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), metering_admin.NoRedirect(),
                                        urllib.request.HTTPSHandler(context=tls))
    pace = RequestPacer()

    def request(path, body=None, headers=None, method=None, opener=client):
        # Fast, zero-delay mock streams can otherwise exhaust the admission
        # bucket. Pace once before each attempt; never replay inference calls.
        pace.wait()
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        req = urllib.request.Request('https://localhost:8443' + path, body, method=method,
                                     headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = opener.open(req, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    code, raw = request('/v1/models')
    assert code == 200
    catalog = json.loads(raw)['data']
    assert [(m['id'], m['praxis']['context'], m['praxis']['output']) for m in catalog] == [
        (m['id'], m['context'], m['output']) for m in models]
    subject = 'routing-' + uuid.uuid4().hex[:12]
    successful = 0
    counted = 0
    os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='pricetag-routing-', dir='/root') as directory:
            keyfile = Path(directory) / 'test.key'
            subprocess.run([sys.executable, ROOT / 'scripts/pricetag/user', '--subject', subject,
                            '--monthly-usd', '100', '--key-lifetime', '1h', '--output', keyfile],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            key = keyfile.read_text().strip()
            for model in models:
                for api in model['apis']:
                    public_aliases = {m['id'] for m in models if api in m['apis']}
                    headers = ({'Authorization': 'Bearer ' + key} if api == 'openai' else
                               {'x-api-key': key, 'anthropic-version': '2023-06-01'})
                    paths = ['/v1/responses', '/v1/chat/completions'] if api == 'openai' else ['/v1/messages']
                    for path in paths:
                        for stream in (False, True):
                            body = {'model': model['id'], 'stream': stream, 'max_tokens': 16,
                                    'messages': [{'role': 'user', 'content': 'routing fixture'}]}
                            if path == '/v1/responses':
                                body['input'] = body.pop('messages')
                                body['max_output_tokens'] = body.pop('max_tokens')
                            elif path == '/v1/chat/completions' and stream:
                                body['stream_options'] = {'include_usage': True}
                            code, payload = request(path, body, headers)
                            assert code == 200, (api, path, stream, code)
                            check(path, payload.decode(), stream, tool=False, model=model['model'])
                            successful += 1
                        spoof = {**headers, 'X-Gateway-Model': 'unknown/provider',
                                 'X-Gateway-Provider': 'unknown'}
                        code, _ = request(path, body, spoof)
                        assert code == 200, ('body must override forged routing headers', code)
                        successful += 1
                        for invalid in invalid_bodies(body, model['model'], public_aliases):
                            code, _ = request(path, invalid, {**headers, 'X-Gateway-Model': model['id']})
                            assert code in (400, 403, 404), ('invalid alias/body', path, code)
                        assert request(path, body)[0] == 401
                        assert request(path, headers=headers, method='GET')[0] == 404
                    if api == 'anthropic':
                        code, payload = request('/v1/messages/count_tokens', {
                            'model': model['id'], 'messages': [{'role': 'user', 'content': 'count'}]}, headers)
                        assert code == 200 and json.loads(payload)['input_tokens'] == 2
                        counted += 1
            assert request('/v1/not-inference', {'model': models[0]['id']},
                           {'Authorization': 'Bearer ' + key})[0] == 404
            print('PASS: catalog limits, native protocols, streaming, count-tokens and forged/invalid aliases', flush=True)

            jar = http.cookiejar.CookieJar()
            browser = urllib.request.build_opener(urllib.request.ProxyHandler({}), metering_admin.NoRedirect(),
                urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=tls))
            code, _ = request('/login', urllib.parse.urlencode({'api_key': key}).encode(),
                              {'Origin': origin, 'Content-Type': 'application/x-www-form-urlencoded'}, opener=browser)
            assert code == 302
            for _ in range(30):
                code, raw = request('/api/v1/me/usage/summary?range=mtd', opener=browser)
                assert code == 200
                overview = json.loads(raw)['overview']
                if overview['total_requests'] >= successful + counted:
                    break
                time.sleep(1)
            # Count-tokens records a zero-token request in the existing gateway.
            assert overview['total_requests'] == successful + counted, ('accounting count', overview['total_requests'], successful + counted)
            assert overview['total_tokens'] == successful * 5
            assert overview['active_users'] == 1
            code, raw = request('/api/v1/me/usage/recent?range=mtd', opener=browser)
            rows = json.loads(raw)
            assert code == 200 and rows and all(row['username'] == subject for row in rows)
            expected_models = {m['model'] for m in models}
            unexpected_rows = [row for row in rows if row['model'] not in expected_models]
            assert not unexpected_rows, unexpected_rows
            print('PASS: usage stays on the authenticated identity and upstream model IDs; count-tokens contributes zero usage; denials not recorded', flush=True)
    finally:
        metering_admin.request('POST', 'key-revocations', {'subject': subject})
        metering_admin.request('PUT', 'users', {'subject': subject, 'active': False})


if __name__ == '__main__':
    main()
