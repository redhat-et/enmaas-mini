#!/usr/bin/env python3
"""Inject synthetic signals and verify each store without publishing query ports."""
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time
import urllib.parse
import urllib.request

spec = importlib.util.spec_from_file_location('lab', Path(__file__).with_name('local.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def send(signal, payload):
    request = urllib.request.Request(f'https://localhost:24443/v1/{signal}',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, context=lab.context(client=True), timeout=10) as response:
        result = json.load(response)
        assert not result.get('partialSuccess'), result


def query(url):
    result = subprocess.run(['podman', 'exec', f'{lab.POD}-prometheus', 'wget', '-qO-', url],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        return None
    return json.loads(result.stdout)


def main():
    now = time.time_ns()
    trace = secrets.token_hex(16)
    marker = 'enmaas-local-'+secrets.token_hex(8)
    resource = {'attributes': [{'key': 'service.name', 'value': {'stringValue': marker}}]}
    send('metrics', {'resourceMetrics': [{'resource': resource, 'scopeMetrics': [{'metrics': [{
        'name': 'enmaas_lab_requests', 'sum': {'aggregationTemporality': 2, 'isMonotonic': True,
        'dataPoints': [{'asInt': '1', 'timeUnixNano': str(now)}]}}]}]}]})
    send('logs', {'resourceLogs': [{'resource': resource, 'scopeLogs': [{'logRecords': [{
        'timeUnixNano': str(now), 'body': {'stringValue': 'SENTINEL_MUST_BE_REMOVED'}, 'attributes': [
            {'key': 'authorization', 'value': {'stringValue': 'SENTINEL_MUST_BE_REMOVED'}}]}]}]}]})
    send('traces', {'resourceSpans': [{'resource': resource, 'scopeSpans': [{'spans': [{
        'traceId': trace, 'spanId': secrets.token_hex(8), 'name': 'SENTINEL_MUST_BE_REMOVED', 'kind': 2,
        'startTimeUnixNano': str(now), 'endTimeUnixNano': str(now+1_000_000), 'attributes': [
            {'key': 'authorization', 'value': {'stringValue': 'SENTINEL_MUST_BE_REMOVED'}}]}]}]}]})
    urls = {
        'metrics': 'http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({'query':'enmaas_lab_requests_total{exported_job="'+marker+'"}'}),
        'logs': 'http://localhost:3100/loki/api/v1/query_range?'+urllib.parse.urlencode({
            'query': '{service_name="'+marker+'"}', 'start':str(now-1_000_000_000)}),
        'traces': f'http://localhost:3200/api/traces/{trace}',
    }
    for signal, url in urls.items():
        for _ in range(30):
            data = query(url)
            if data and (data.get('data', {}).get('result') if signal != 'traces' else data.get('batches') or data.get('resourceSpans')):
                assert 'SENTINEL_MUST_BE_REMOVED' not in json.dumps(data), signal
                print(f'PASS: {signal} stored and queryable; sensitive test attribute absent')
                break
            time.sleep(2)
        else:
            raise SystemExit(f'FAIL: {signal} not queryable within 60s')

if __name__ == '__main__':
    main()
