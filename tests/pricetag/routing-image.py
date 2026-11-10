#!/usr/bin/env python3
"""Isolated native router qualification; deployed auth/accounting has its own test.

Two connections expose the same upstream ID with different credentials. All
provider traffic stays in test containers; PRAXIS_IMAGE selects the release pin.
"""
import importlib.util
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


def main():
    render = unified_config.render

    def candidate_render(*args, **kwargs):
        result = render(*args, **kwargs, intelligent_routing=True)
        result['admin']['metrics_address'] = '127.0.0.1:9902'
        # Match PriceTag's absence of the legacy token limiter. The pinned
        # pipeline validator only recognizes router as its logical-upstream
        # binder; this test does not enable intelligent routing for all-in-one.
        for chain in result['filter_chains']:
            chain['filters'] = [f for f in chain['filters']
                                if f['filter'] not in ('token_rate_limit', 'policy', 'rate_limit')]
        return result

    with patch.object(unified_config, 'render', candidate_render):
        with fixture.UnifiedGateway('all-in-one', 'valkey') as gateway:
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
    print('PASS: duplicate upstream IDs route to distinct credential-checked providers; native streams/tools and invalid aliases')


if __name__ == '__main__':
    main()
