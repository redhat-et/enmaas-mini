#!/usr/bin/env python3
"""Verify native routing, alias boundaries and accounting on a mock deployment."""
import http.cookiejar
import http.client
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
from decimal import Decimal, ROUND_HALF_UP

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
    prices = {row['Model']: row for row in metering_admin.request('GET', 'prices')}
    expected_cost = Decimal(0)
    os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='pricetag-routing-', dir='/root') as directory:
            keyfile = Path(directory) / 'test.key'
            subprocess.run([sys.executable, ROOT / 'scripts/pricetag/user', '--subject', subject,
                            '--monthly-usd', '100', '--key-lifetime', '1h', '--output', keyfile],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            key = keyfile.read_text().strip()
            # Real WebSocket GETs and upgrade-bearing POSTs must be rejected at
            # the public edge, including discovery and dashboard routes. Test
            # anonymous and valid callers; no upstream or ledger event is due.
            for auth in ({}, {'Authorization': 'Bearer ' + key, 'x-api-key': key}):
                for path in ('/v1/models', '/v1/responses', '/v1/chat/completions',
                             '/v1/messages', '/v1/messages/count_tokens', '/login'):
                    for method in ('GET', 'POST'):
                        for protocol in ('websocket', 'h2c', 'custom/1', 'h2c, WebSocket'):
                            connection = http.client.HTTPSConnection('localhost', 8443, context=tls, timeout=10)
                            try:
                                connection.request(method, path,
                                    json.dumps({'model': models[0]['id']}) if method == 'POST' else None,
                                    {**auth, 'Content-Type': 'application/json',
                                     'Connection': 'keep-alive, Upgrade', 'Upgrade': protocol,
                                     'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='})
                                response = connection.getresponse()
                                payload = response.read()
                                assert response.status == 400, ('public upgrade', path, method, protocol, response.status)
                                assert json.loads(payload)['error']['code'] == 'upgrade_not_supported'
                            finally:
                                connection.close()
            print('PASS: public upgrade attempts rejected for anonymous and authenticated callers', flush=True)
            for model in models:
                before_model = successful
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
                    if api == 'openai':
                        for options in (None, {}, {'include_usage': False}, {'include_usage': True},
                                        {'include_usage': False, 'include_obfuscation': False}):
                            body = {'model': model['id'], 'stream': True,
                                    'messages': [{'role': 'user', 'content': 'streaming accounting'}]}
                            if options is not None:
                                body['stream_options'] = options
                            code, payload = request('/v1/chat/completions', body, headers)
                            assert code == 200, ('streaming accounting', options, code)
                            check('/v1/chat/completions', payload.decode(), True, tool=False, model=model['model'])
                            successful += 1
                    if api == 'anthropic':
                        code, payload = request('/v1/messages/count_tokens', {
                            'model': model['id'], 'messages': [{'role': 'user', 'content': 'count'}]}, headers)
                        assert code == 200 and json.loads(payload)['input_tokens'] == 2
                        counted += 1
                # The pinned PriceTag ledger uses $15/$75 per million when no
                # price row exists. Mock setup intentionally allows that path;
                # do not mutate shared prices merely to make this test pass.
                price = prices.get(model['model'], {'InputCost': 15, 'OutputCost': 75})
                unit_cost = ((2 * Decimal(str(price['InputCost'])) + 3 * Decimal(str(price['OutputCost'])))
                             / Decimal(1000000)).quantize(Decimal('0.00000001'))
                expected_cost += (successful - before_model) * unit_cost
            assert request('/v1/not-inference', {'model': models[0]['id']},
                           {'Authorization': 'Bearer ' + key})[0] == 404
            print('PASS: catalog limits, native protocols, streaming opt-in variants, count-tokens and forged/invalid aliases', flush=True)

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
            # The overview rounds dollars to cents for display. The user's
            # quota view exposes the unrounded spend used for enforcement.
            assert Decimal(str(overview['total_cost_usd'])) == expected_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            code, raw = request('/api/v1/whoami', opener=browser)
            assert code == 200
            quota = json.loads(raw)['quota']
            assert quota['username'] == subject
            assert abs(Decimal(str(quota['spent_usd'])) - expected_cost) <= Decimal('0.0000001'), (
                'USD accounting differs from configured/fallback rates', quota['spent_usd'], str(expected_cost))
            code, raw = request('/api/v1/me/usage/recent?range=mtd', opener=browser)
            rows = json.loads(raw)
            assert code == 200 and rows and all(row['username'] == subject for row in rows)
            assert all(row['model'] in {m['model'] for m in models} for row in rows)
            print('PASS: exact request/token/USD accounting on the authenticated identity and upstream IDs; count-tokens contributes zero usage; denials not recorded', flush=True)
    finally:
        metering_admin.request('POST', 'key-revocations', {'subject': subject})
        metering_admin.request('PUT', 'users', {'subject': subject, 'active': False})


if __name__ == '__main__':
    main()
