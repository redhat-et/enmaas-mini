#!/usr/bin/env python3
"""Fresh, local-only runtime qualification; never accepts AWS credentials or endpoints."""
import argparse
import importlib.util
import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def evidence_window(phases):
    if not phases:raise ValueError('No measured phases')
    start=datetime.datetime.fromisoformat(phases[0]['started_utc']).timestamp()
    end=int(datetime.datetime.fromisoformat(phases[-1]['ended_utc']).timestamp())-30
    if end<=start:raise ValueError('Measured window is too short')
    return end,math.ceil((end-start)/60)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration',type=int,choices=(65,600),default=65)
    args=parser.parse_args()
    if os.environ.get('ENMAAS_LAB_INSTANCE')!='external-ci':
        parser.error('Requires an explicitly selected external-ci fixture')
    if (ROOT/'.state/external-ci').exists():parser.error('Requires fresh state; existing fixtures are never reset')
    spec=importlib.util.spec_from_file_location('lab',HERE/'local.py')
    lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
    def run(file,*arguments):
        subprocess.run([sys.executable,str(HERE/file),*arguments],check=True,cwd=ROOT)
    image=lab.IMAGES['praxis']
    run('local.py','start','--gateway')
    run('gateway-sso.py','start','--praxis-image',image,'--metering-image','localhost/pricetag-metering:upstream-2281e1a',
        '--maas-image','localhost/pricetag-maas:standalone','--mock-profiles')
    run('gateway-sso.py','test')
    run('gateway-sso.py','promote')
    run('gateway-signals.py','--praxis-image',image)
    run('metric-freshness.py')
    run('local-benchmark.py','--praxis-image',image,'--scenario','sustained','--duration',str(args.duration),'--min-free-disk-percent','10','--min-free-disk-gib','8')
    reports=sorted(lab.STATE.glob('lab-run-*'),key=lambda path:path.stat().st_mtime)
    run('monitoring-result.py',str(reports[-1]))
    run('build-stress-provider.py')
    spec=importlib.util.spec_from_file_location('stress_builder',HERE/'build-stress-provider.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    run('local-stress.py','--praxis-image',image,'--provider-image',builder.inputs()[1],
        '--duration','65','--max-rate','10','--concurrency','512','--min-free-disk-percent','10')
    reports=sorted(lab.STATE.glob('lab-run-*'),key=lambda path:path.stat().st_mtime)
    run('stress-result.py',str(reports[-1]))
    result=json.loads((reports[-1]/'summary.json').read_text())
    evidence_time,evidence_minutes=evidence_window(result['phases'])
    run('signals.py')
    run('log-runtime.py')
    run('local-host-metrics.py','--samples','2','--interval','1')
    env=dict(os.environ,NODE_PATH=str(ROOT/'tests/pricetag/browser/node_modules'),NODE_EXTRA_CA_CERTS=str(lab.STATE/'ca.pem'),ENMAAS_VERIFY_REALISTIC='1')
    for script in ('monitoring-dashboard.cjs','trace-dashboard.cjs','logs-dashboard.cjs','metering-dashboard.cjs','organization-dashboard.cjs','browser.cjs'):
        arguments=['--require-stress','--require-continuous-data','--at',str(evidence_time),'--window-minutes',str(evidence_minutes)] if script=='monitoring-dashboard.cjs' else []
        subprocess.run(['node',str(HERE/script),str(lab.STATE),*arguments],env=env,check=True,cwd=ROOT)
    print('PASS: local fixture transport, mock accounting, monitoring queries and browser boundaries')

if __name__=='__main__':main()
