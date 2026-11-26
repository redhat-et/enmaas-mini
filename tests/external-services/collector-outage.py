#!/usr/bin/env python3
"""Bounded local receiver outage: mock inference/accounting and recovery only.

Stops and restores the existing labeled collector container, retaining volumes.
Does not qualify telemetry losslessness, queue exhaustion or multi-host outages.
"""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import urllib.parse

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('outage_bench', HERE/'local-benchmark.py')
bench = importlib.util.module_from_spec(spec); spec.loader.exec_module(bench)


@contextmanager
def interruption_guard():
    """Allow ordinary cancellation while leaving time for restoration."""
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    def interrupt(number, frame):
        for protected in previous:
            signal.signal(protected, signal.SIG_IGN)
        raise InterruptedError('Outage test cancelled; restoring local fixture')
    try:
        for number in previous:
            signal.signal(number, interrupt)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def fresh_gateway_signals(metrics, traces, started):
    fresh_metric = any(float(row['value'][1]) >= started
                       for row in metrics.get('data', {}).get('result', []))
    fresh_trace = any(int(row.get('startTimeUnixNano', '0'))/1e9 >= started
                      for row in traces.get('traces', []))
    return fresh_metric and fresh_trace


def verify_gateway_recovery(controller, gateway_id):
    started = time.time()
    with controller.transport() as request:
        if request(0, 10)['status'] != 200:
            raise RuntimeError('Post-recovery inference failed')
    subject = controller.callers[0][0]
    controller.summary['completed_mock_requests'][subject] += 1
    controller.save()
    metric_url = 'http://localhost:9090/api/v1/query?' + urllib.parse.urlencode({
        'query':'timestamp(praxis_http_requests_total{cluster="authenticated-openai"})'})
    for _ in range(30):
        trace_url = 'http://localhost:3200/api/search?' + urllib.parse.urlencode({
            'tags':'service.name='+controller.service_name, 'start':int(started),
            'end':int(time.time())+1, 'limit':20})
        metrics = bench.signals.query(metric_url) or {}
        traces = bench.signals.query(trace_url) or {}
        if fresh_gateway_signals(metrics, traces, started):
            current = json.loads(bench.fixture.run('inspect', bench.lab.POD+'-gateway-sso'))[0]
            if current['Id'] != gateway_id:
                raise RuntimeError('Gateway was replaced during recovery test')
            if not controller.verify():
                raise RuntimeError('Post-recovery accounting did not reconcile')
            return
        time.sleep(2)
    raise RuntimeError('Fresh gateway metrics/traces missing after receiver recovery')


def main(seconds):
    if not 10 <= seconds <= 120:
        raise ValueError('Choose an outage between 10 and 120 seconds')
    lab = bench.lab
    gateway = json.loads(bench.fixture.run('inspect', lab.POD+'-gateway-sso'))[0]
    controller = bench.LocalController(gateway['Image'])
    collector = json.loads(bench.fixture.run('inspect', lab.POD+'-collector'))[0]
    if (collector['Config'].get('Labels', {}).get('io.enmaas.test-lab') != lab.INSTANCE
            or not collector['State']['Running']):
        raise ValueError('Collector must be running and owned by this named fixture')
    identifier = collector['Id']
    stopped = False
    recovered = False
    try:
        controller.guard()
        controller.prepare(1)  # exclusive benchmark lock, mock-only backend checks
        if not controller.telemetry:
            raise ValueError('Gateway telemetry must be enabled')
        stopped = True  # restore even if the stop reply is lost
        bench.fixture.run('stop', '--time', '5', identifier)
        if json.loads(bench.fixture.run('inspect', identifier))[0]['State']['Running']:
            raise RuntimeError('Collector did not stop')
        started = time.monotonic()
        completed = 0
        with controller.transport() as request:
            while time.monotonic()-started < seconds:
                result = request(0, 10)
                if result['status'] != 200:
                    raise RuntimeError('Inference failed while collector was unavailable')
                subject = controller.callers[0][0]
                controller.summary['completed_mock_requests'][subject] += 1
                completed += 1
                time.sleep(1)
        controller.save()
        if completed < 1 or not controller.verify():
            raise RuntimeError('Accounting did not reconcile during receiver outage')
        bench.fixture.run('start', identifier)
        lab.check()
        subprocess.run(['python3', str(HERE/'signals.py')], check=True, timeout=120)
        verify_gateway_recovery(controller, gateway['Id'])
        recovered = True
        lab.write(controller.output, 'collector-outage.json', {
            'passed': True, 'outage_seconds': seconds, 'completed_requests': completed,
            'ledger_verified_during_outage': True, 'fresh_signals_after_recovery': True,
            'gateway_export_recovered_without_restart': True, 'post_recovery_requests': 1,
            'telemetry_losslessness_qualified': False})
    finally:
        try:
            if stopped and not recovered:
                bench.fixture.run('start', identifier)
                lab.check()
        finally:
            if not controller.cleanup():
                raise RuntimeError('Test cleanup incomplete; inspect private state')
    print('PASS: inference and exact accounting during collector outage; fresh telemetry after recovery.')
    print('Private evidence:', controller.output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        with interruption_guard():
            main(args.seconds)
    except Exception as error:
        raise SystemExit(type(error).__name__+': outage qualification failed; inspect private state') from None
