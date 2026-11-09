#!/usr/bin/env python3
"""Isolated native router qualification; deployed auth/accounting has its own test.

Two connections expose the same upstream ID with different credentials. All
provider traffic stays in test containers; PRAXIS_IMAGE selects the release pin.
"""
import importlib.util
import argparse
import http.client
import json
import os
from pathlib import Path
from unittest.mock import patch

if not os.environ.get('PRAXIS_IMAGE'):
    raise SystemExit('Set PRAXIS_IMAGE to the PriceTag deployment image before running this test')

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('unified_image', ROOT / 'tests/rhel/unified-image.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
import unified_config
spec = importlib.util.spec_from_file_location('pricetag_gateway', ROOT / 'scripts/pricetag/gateway.py')
pricetag_gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricetag_gateway)


def upgrade_contract(gateway):
    def counts():
        return [len(json.loads(fixture.run(fixture.ENGINE, 'exec', gateway.name + '-mock', 'python3', '-c',
            'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:' + str(port) +
            '/state").read().decode())'))['records']) for port in (19000, 19001, 19002)]
    before = counts()
    for path, alias in (('/v1/responses', 'openai/gpt-5.4-mini'),
                        ('/v1/chat/completions', 'team/gpt-5.4-mini'),
                        ('/v1/messages', 'team/gpt-5.4-mini'),
                        ('/v1/messages/count_tokens', 'team/gpt-5.4-mini')):
        for protocol in ('websocket', 'WebSocket', 'h2c', 'custom/1', 'h2c, websocket'):
            for method in ('GET', 'POST'):
                # urllib forces Connection: close; use http.client so these are
                # real upgrade handshakes on the wire, not just header probes.
                port = gateway.anthropic_port if path.startswith('/v1/messages') else gateway.openai_port
                connection = http.client.HTTPConnection('127.0.0.1', int(port), timeout=10)
                try:
                    connection.request(method, path, json.dumps({'model': alias}) if method == 'POST' else None,
                        {'Content-Type': 'application/json', 'Connection': 'keep-alive, Upgrade', 'Upgrade': protocol,
                         'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='})
                    response = connection.getresponse()
                    payload = response.read()
                    assert response.status == 400, ('upgrade accepted', path, method, protocol, response.status)
                    assert json.loads(payload)['error']['code'] == 'upgrade_not_supported'
                finally:
                    connection.close()
    assert counts() == before, 'rejected upgrades reached a provider'
    print('PASS: native listeners reject all tested upgrade protocols before any provider request', flush=True)


def streaming_contract(gateway):
    for model in fixture.MODELS:
        if 'openai' not in model['apis']:
            continue
        for options in (None, {}, {'include_usage': False}, {'include_usage': True},
                        {'include_usage': False, 'include_obfuscation': False}):
            body = fixture.request_body('/v1/chat/completions', stream=True, tools=True)
            body['model'] = model['id']
            body.pop('stream_options')
            if options is not None:
                body['stream_options'] = options
            if model['model'].startswith('gpt-'):
                body['max_completion_tokens'] = body.pop('max_tokens')
            status, payload, _ = gateway.request('/v1/chat/completions', body=body)
            assert status == 200, (model['id'], options, status)
            fixture.check('/v1/chat/completions', payload, True, tool=True, model=model['model'])
    print('PASS: Chat streams include usage when omitted, false or already requested; tools preserved', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', choices=('all', 'upgrades', 'streaming'), default='all')
    args = parser.parse_args()
    render = unified_config.render

    def candidate_render(*args, **kwargs):
        result = render(*args, **kwargs, intelligent_routing=True)
        # Match PriceTag's absence of the legacy token limiter. The pinned
        # pipeline validator only recognizes router as its logical-upstream
        # binder; this test does not enable intelligent routing for all-in-one.
        for chain in result['filter_chains']:
            chain['filters'] = pricetag_gateway.inference_guards(chain['name']) + [f for f in chain['filters']
                                if f['filter'] not in ('token_rate_limit', 'policy', 'rate_limit')]
        return result

    with patch.object(unified_config, 'render', candidate_render):
        with fixture.UnifiedGateway('all-in-one', 'valkey') as gateway:
            if args.check in ('all', 'upgrades'):
                upgrade_contract(gateway)
            if args.check in ('all', 'streaming'):
                streaming_contract(gateway)
            if args.check != 'all':
                return
            for chain in gateway.config['filter_chains']:
                names = [f['filter'] for f in chain['filters']]
                assert names.count('intelligent_route') == 1 and 'router' not in names
            for model in fixture.MODELS:
                for api in model['apis']:
                    paths = ['/v1/messages'] if api == 'anthropic' else ['/v1/responses', '/v1/chat/completions']
                    for path in paths:
                        for stream in (False, True):
                            body = fixture.request_body(path, stream, tools=True)
                            body['model'] = model['id']
                            if path == '/v1/chat/completions' and model['model'].startswith('gpt-'):
                                body['max_completion_tokens'] = body.pop('max_tokens')
                            status, payload, _ = gateway.request(path, body=body)
                            assert status == 200, (model['id'], path, stream, status)
                            fixture.check(path, payload, stream, tool=True, model=model['model'])
            for path in ('/v1/responses', '/v1/chat/completions', '/v1/messages'):
                for body in (b'{"model":"unknown"}', b'{}', b'{"model":'):
                    status, _ = gateway.raw(path, body, **{'X-Gateway-Model': 'openai/gpt-5.4-mini'})
                    assert status in (400, 404), (path, status)
            # Records contain only booleans and model IDs, never raw credentials.
            for port, upstream in ((19000, 'gpt-5.4-mini'), (19002, 'gpt-5.4-mini'), (19001, 'qwen3-8b')):
                records = json.loads(fixture.run(fixture.ENGINE, 'exec', gateway.name + '-mock', 'python3', '-c',
                    'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:' + str(port) +
                    '/state").read().decode())'))['records']
                assert records and any(r['model'] == upstream for r in records)
                assert all(r['credential_ok'] and r['classification_clean'] for r in records)
                assert any(r['stream'] for r in records) and any(not r['stream'] for r in records)
                chats = [r for r in records if r['path'] == '/v1/chat/completions' and r['stream']]
                assert chats and all(r['stream_options']['include_usage'] for r in chats)
                assert any(r['stream_options'].get('include_obfuscation') is False for r in chats)
                assert all(r['stream_options'] is None for r in records if r['path'] != '/v1/chat/completions')
                assert all(r['stream_options'] is None for r in records if not r['stream'])
    print('PASS: duplicate upstream IDs route to distinct credential-checked providers; native streams/tools and invalid aliases')


if __name__ == '__main__':
    main()
