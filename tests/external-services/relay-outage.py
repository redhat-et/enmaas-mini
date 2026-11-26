#!/usr/bin/env python3
"""Bounded local relay outage: verify stale-data alerts, browser rows and recovery."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.parse
import signals


def alerts():
    data=signals.query('http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({'query':'ALERTS{alertstate="firing",alertname=~"Enmaas.*MetricsStale"}'})) or {}
    return {row['metric']['alertname'] for row in data.get('data',{}).get('result',[])}


def main():
    lab=signals.lab;lab.check_port_bindings()
    name=lab.POD+'-gateway-relay'
    record=json.loads(subprocess.check_output(['podman','inspect',name]))[0]
    if record['Config'].get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE or not record['State']['Running'] or record['State'].get('Paused'):
        raise ValueError('Requires an owned, running, unpaused relay')
    lock=os.open(lab.STATE/'.relay-outage.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def interrupted(*_):raise InterruptedError('Local fault test interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    started=datetime.datetime.now(datetime.timezone.utc).isoformat()
    verified=False
    try:
        subprocess.run(['podman','pause',record['Id']],check=True,stdout=subprocess.DEVNULL,timeout=10)
        print('Paused owned local telemetry relay; inference/accounting services remain running',flush=True)
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            if {'EnmaasGatewayMetricsStale','EnmaasAccountingMetricsStale'}<=alerts():break
            time.sleep(3)
        else:raise RuntimeError('Stale-source alerts failed to fire')
        print('PASS: gateway and accounting source timestamps raised critical stale-data alerts',flush=True)
        env=dict(os.environ,NODE_PATH=str(lab.ROOT/'tests/pricetag/browser/node_modules'),NODE_EXTRA_CA_CERTS=str(lab.STATE/'ca.pem'))
        subprocess.run(['node',str(Path(__file__).with_name('monitoring-dashboard.cjs')),str(lab.STATE),'--only-main','--require-stale-alert','--record'],env=env,check=True,timeout=90)
        verified=True
    finally:
        subprocess.run(['podman','unpause',record['Id']],check=True,stdout=subprocess.DEVNULL,timeout=10)
        os.close(lock)
        with (lab.STATE/'relay-outages.jsonl').open('a') as output:
            output.write(json.dumps({'started':started,'resumed':datetime.datetime.now(datetime.timezone.utc).isoformat(),'browser_verified':verified})+'\n')
        print('Local telemetry relay resumed',flush=True)
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        if not alerts():break
        time.sleep(3)
    else:raise RuntimeError('Stale-source alerts did not recover')
    lab.write(lab.STATE,'relay-outage-evidence.json',{'started':started,'finished':datetime.datetime.now(datetime.timezone.utc).isoformat(),
      'actual_relay_pause':True,'gateway_stale_alert':True,'accounting_stale_alert':True,'browser_verified':True,'recovered':True})
    print('PASS: critical telemetry alerts cleared after the relay resumed')

if __name__=='__main__':main()
