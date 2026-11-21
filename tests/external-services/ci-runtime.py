#!/usr/bin/env python3
"""Fresh, local-only runtime qualification; never accepts AWS credentials or endpoints."""
import argparse
import importlib.util
import datetime
import json
import math
import os
import re
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


def load(file, name):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def qualify(lab, args, env):
    runtime=load('security-runtime.py','bounded_runtime')
    def run(file,*arguments):
        runtime.run_check(HERE/file,list(arguments),cwd=ROOT,env=env,
            timeout=max(480,args.duration*6+600),cleanup_timeout=120)
    try:
        image=lab.IMAGES['praxis']
        run('local.py','start','--gateway')
        run('gateway-sso.py','start','--praxis-image',image,'--metering-image',args.metering_image,
            '--maas-image',args.maas_image,'--mock-profiles')
        run('gateway-sso.py','test')
        run('gateway-sso.py','promote')
        run('gateway-signals.py','--praxis-image',image)
        run('metric-freshness.py')
        run('local-benchmark.py','--praxis-image',image,'--scenario','sustained','--duration',str(args.duration),'--min-free-disk-percent','10','--min-free-disk-gib','8')
        reports=sorted(lab.STATE.glob('lab-run-*'),key=lambda path:path.stat().st_mtime)
        run('monitoring-result.py',str(reports[-1]))
        run('build-stress-provider.py')
        run('build-stress-generator.py')
        generator=load('build-stress-generator.py','ci_generator_builder')
        spec=importlib.util.spec_from_file_location('stress_builder',HERE/'build-stress-provider.py')
        builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        run('local-stress.py','--praxis-image',image,'--provider-image',builder.inputs()[1],
            '--generator-image',generator.inputs()[1],'--workers','2','--generator-memory-mib','512',
            '--duration','65','--max-rate','10','--concurrency','1024','--min-free-disk-percent','10')
        reports=sorted(lab.STATE.glob('lab-run-*'),key=lambda path:path.stat().st_mtime)
        run('stress-result.py',str(reports[-1]))
        # Benchmarks retire their callers. The organization table lists active
        # identities, so explicitly prepare the existing display-only Alice team.
        # Its helper requires revoked/expired keys and verifies scoped usage.
        subprocess.run(['node',str(HERE/'alice-team.cjs'),str(lab.STATE),str(reports[-1]),'--apply'],
            env=env,check=True,cwd=ROOT,timeout=360)
        result=json.loads((reports[-1]/'summary.json').read_text())
        evidence_time,evidence_minutes=evidence_window(result['phases'])
        run('signals.py')
        run('log-runtime.py')
        run('local-host-metrics.py','--samples','2','--interval','1')
        for script in ('monitoring-dashboard.cjs','trace-dashboard.cjs','logs-dashboard.cjs','metering-dashboard.cjs','organization-dashboard.cjs','browser.cjs'):
            arguments=['--require-stress','--require-continuous-data','--at',str(evidence_time),'--window-minutes',str(evidence_minutes)] if script=='monitoring-dashboard.cjs' else []
            subprocess.run(['node',str(HERE/script),str(lab.STATE),*arguments],env=env,check=True,cwd=ROOT,timeout=360)
    finally:
        runtime.run_check(HERE/'local.py',['down'],cwd=ROOT,env=env,timeout=120)
    lab.write(lab.STATE,'benchmark-qualification.json',{
        'passed':True,'phase_seconds':args.duration,
        'not_qualified':['RHEL','AWS','public access','maximum capacity']})
    print('PASS: mock accounting, streaming, monitoring queries and browser boundaries; owned resources cleaned')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration',type=int,choices=(65,600),default=65)
    parser.add_argument('--metering-image',default='localhost/pricetag-metering:upstream-2281e1a')
    parser.add_argument('--maas-image',default='localhost/pricetag-maas:standalone')
    args=parser.parse_args()
    instance=os.environ.get('ENMAAS_LAB_INSTANCE','')
    if not re.fullmatch(r'external-ci(?:-[a-f0-9]{8,12})?',instance):
        parser.error('Requires external-ci or external-ci-<8 to 12 hex characters>')
    if (ROOT/'.state'/instance).exists():
        parser.error('Requires fresh state; existing fixtures are never reset')
    lab=load('local.py','lab')
    existing=subprocess.run(['podman','pod','exists',lab.POD],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    if existing.returncode!=1:
        parser.error('Cannot establish a fresh pod; existing pods are never reset')
    for name in ('db','perses','prometheus','loki','tempo'):
        volume=subprocess.run(['podman','volume','exists',lab.POD+'-'+name],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        if volume.returncode!=1:
            parser.error('Cannot establish fresh volumes; retained data is never reused or deleted')
    env=dict(os.environ,NODE_EXTRA_CA_CERTS=str(lab.STATE/'ca.pem'),ENMAAS_VERIFY_REALISTIC='1')
    env.setdefault('NODE_PATH',str(ROOT/'tests/pricetag/browser/node_modules'))
    qualify(lab,args,env)


if __name__=='__main__':
    os.umask(0o077)
    try:main()
    except (OSError,ValueError,subprocess.SubprocessError):
        raise SystemExit('Benchmark qualification failed; inspect the selected fixture and private evidence') from None
