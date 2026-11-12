#!/usr/bin/env python3
"""Qualify real Praxis metrics/traces through the local mTLS relay and stores."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import secrets
import time
import urllib.parse
import urllib.request
import yaml

HERE=Path(__file__).resolve().parent

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

sso=load('sso','gateway-sso.py');agent=load('agent','telemetry-agent.py');signals=load('signals','signals.py')
lab=sso.lab


def relay_runtime_options():
    # The local metrics/trace relay does not mount application logs. Its shared
    # configuration still initializes the file receiver; keep its offsets in
    # bounded disposable memory. Persistent log-offset tests use log-runtime.py.
    return ['--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '256m', '--pids-limit', '128',
            '--tmpfs', '/positions:rw,noexec,nosuid,size=16m,mode=1777']


def main(praxis):
    config=agent.configuration('10.0.0.20')
    # Fixture services share one trusted namespace. Production uses private-IP
    # routing between hosts; only the test destination and scrape address differ.
    config['exporters']['otlphttp/remote']['endpoint']='https://localhost:24443'
    config['receivers']['prometheus']['config']['scrape_configs'][1]['static_configs'][0]['targets']=['127.0.0.1:9908']
    config['service']['telemetry']={'metrics':{'level':'none'}} # receiving collector owns the fixture's 8888
    lab.write(lab.STATE,'gateway-relay.yaml',yaml.safe_dump(config,sort_keys=False))
    for name,file in [('gateway-relay',lab.STATE/'gateway-relay.yaml'),('gateway-client',lab.STATE/'client.pem'),('gateway-client-key',lab.STATE/'client.key')]:
        sso.secret(name,file)
    sso.run('run','--detach','--pod',lab.POD,'--name',lab.POD+'-gateway-relay','--label',lab.LABEL,
        *relay_runtime_options(),
        '--secret',lab.secret_name('gateway-relay')+',target=/lab/collector.yaml,mode=0444',
        '--secret',lab.secret_name('ca.pem')+',target=/lab/server-ca.pem,mode=0444',
        '--secret',lab.secret_name('gateway-client')+',target=/lab/client.pem,mode=0444',
        '--secret',lab.secret_name('gateway-client-key')+',target=/lab/client.key,mode=0444',
        lab.IMAGES['collector'],'--config=/lab/collector.yaml')
    sso.run('rm','-f',lab.POD+'-gateway-sso')
    service='enmaas-gateway-'+secrets.token_hex(6)
    sso.start_gateway(praxis,telemetry=True,service_name=service)
    lab.wait_url('https://localhost:28443/login')
    for _ in range(10):
        request=urllib.request.Request('https://localhost:28443/login',headers={'Authorization':'Bearer SENTINEL_GATEWAY_HEADER','Cookie':'irrelevant=SENTINEL_GATEWAY_COOKIE'})
        with urllib.request.urlopen(request,context=lab.context(),timeout=5) as response:
            assert response.status==200
        time.sleep(.2)
    query='praxis_http_requests_total{cluster=~"dashboard|metering"}'
    metric_url='http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({'query':query})
    trace_url='http://localhost:3200/api/search?'+urllib.parse.urlencode({'tags':'service.name='+service,'limit':20})
    found={}
    for _ in range(40):
        metric=signals.query(metric_url)
        traces=signals.query(trace_url)
        if metric:
            rows=metric.get('data',{}).get('result',[])
            if {row['metric'].get('cluster') for row in rows}=={'dashboard','metering'} and all(float(row['value'][1])>=10 for row in rows):
                found['metrics']=True
        if traces and traces.get('traces'):
            for trace in traces['traces']:
                data=signals.query('http://localhost:3200/api/traces/'+trace['traceID'])
                if data and 'SENTINEL_GATEWAY_' not in json.dumps(data):
                    found['traces']=True
                else:raise RuntimeError('Trace details absent or request sentinel leaked')
        if len(found)==2:break
        time.sleep(2)
    lab.write(lab.STATE,'gateway-signals-evidence.json',{'passed':len(found)==2,'received':sorted(found),
        'traffic':'ten real gateway login GET requests','inference_accounting_qualified':False})
    if len(found)!=2:raise RuntimeError('Real gateway signal missing: '+','.join(sorted({'metrics','traces'}-set(found))))
    print('PASS: real gateway metrics and traces arrived through the mTLS relay')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--praxis-image',required=True)
    args=parser.parse_args()
    try:main(args.praxis_image)
    except (RuntimeError,OSError,subprocess.SubprocessError):
        raise SystemExit('Gateway signal qualification failed; inspect private fixture evidence and logs') from None
