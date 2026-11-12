#!/usr/bin/env python3
"""Populate every realistic mock alias through its native API and verify billing.

Local named fixture only. Uses temporary ordinary-user credentials; no real
provider keys, direct business-data SQL writes or host service management.
"""
from decimal import Decimal, ROUND_HALF_UP
import importlib.util
import json
import os
from pathlib import Path
import re
import time
import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('profile_local_benchmark', HERE / 'local-benchmark.py')
bench = importlib.util.module_from_spec(spec); spec.loader.exec_module(bench)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Mock inference redirects are refused')


def database(query):
    return bench.fixture.run('exec', bench.lab.POD + '-db', 'psql', '-U', 'keycloak',
        '-d', 'metering_sso', '-At', '-v', 'ON_ERROR_STOP=1', '-c', query).decode().strip()


def request_cost(model):
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', model):
        raise ValueError('Unreviewed model identifier')
    row = database("SELECT input_cost_per_mtok,output_cost_per_mtok FROM model_pricing WHERE model='" + model + "';").split('|')
    if len(row) != 2:
        raise ValueError('No explicit imported price for configured mock model')
    return ((Decimal(row[0])*2 + Decimal(row[1])*3)/1000000).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)


def request_headers(api, key):
    if api not in ('openai', 'anthropic'):
        raise ValueError('Unsupported native API')
    headers = {'Content-Type': 'application/json'}
    if api == 'anthropic':
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    else:
        headers['Authorization'] = 'Bearer ' + key
    return headers


def main(image):
    if not (bench.lab.STATE / 'mock-profiles.json').is_file():
        raise ValueError('This test requires the realistic mock-profile fixture')
    controller = bench.LocalController(image)
    counts = {}; rates = {}; checks = []
    try:
        controller.prepare(1)
        from mock_profiles import configuration
        models = configuration()['models']
        rates = {m['model']: request_cost(m['model']) for m in models}
        subject, key = controller.callers[0]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=bench.lab.context()))
        for model in models:
            for api in model['apis']:
                print('Probe: ' + model['id'] + ' (' + api + ')', flush=True)
                path = '/v1/messages' if api == 'anthropic' else '/v1/chat/completions'
                payload = {'model': model['id'], 'messages': [{'role': 'user', 'content': 'mock model catalog demonstration'}],
                           'max_tokens': 32, 'stream': False}
                request = urllib.request.Request('https://localhost:28443' + path,
                    data=json.dumps(payload).encode(), headers=request_headers(api, key))
                with opener.open(request, timeout=10) as response:
                    data = response.read(65537)
                    if response.status != 200 or len(data) > 65536:
                        raise ValueError('Unexpected mock reply')
                result = json.loads(data)
                usage = result.get('usage', {})
                fields = ('input_tokens','output_tokens') if api == 'anthropic' else ('prompt_tokens','completion_tokens')
                if (usage.get(fields[0]), usage.get(fields[1])) != (2,3):
                    raise ValueError('Unexpected native usage contract')
                counts[model['model']] = counts.get(model['model'], 0) + 1
                checks.append({'alias': model['id'], 'api': api})
        if not re.fullmatch(r'lab-run-[a-f0-9]{32}-0', subject):
            raise ValueError('Unexpected isolated test identity')
        for model, count in counts.items():
            query = ("SELECT count(*),coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0),"
                     "count(cost_usd),coalesce(sum(cost_usd),0) FROM usage_events WHERE username='" + subject + "' AND model='" + model + "';")
            for _ in range(30):
                fields = database(query).split('|')
                if (tuple(map(int, fields[:4])) == (count, count*2, count*3, count)
                        and Decimal(fields[4]) == rates[model]*count):
                    break
                time.sleep(1)
            else:
                raise ValueError('Native mock model ledger differs')
        bench.lab.write(bench.lab.STATE, 'profile-inference-evidence.json',
                        {'passed': True, 'checks': checks, 'requests': sum(counts.values()), 'ledger_verified': True})
    finally:
        if not controller.cleanup():
            raise RuntimeError('Profile test cleanup incomplete; inspect private fixture state')
    print('PASS: all mock aliases/native APIs and per-model imported-price accounting; test key retired')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--praxis-image', required=True)
    args = parser.parse_args(); os.umask(0o077)
    try:
        main(args.praxis_image)
    except urllib.error.HTTPError as error:
        raise SystemExit('HTTP '+str(error.code)+': native mock request failed; no credentials printed') from None
    except Exception as error:
        raise SystemExit(type(error).__name__ + ': profile inference qualification failed; inspect private fixture state') from None
