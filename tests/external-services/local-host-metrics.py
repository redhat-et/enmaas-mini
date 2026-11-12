#!/usr/bin/env python3
"""Sample the Podman Linux host and send aggregates into the local mTLS fixture."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import shlex
import time

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('signals',HERE/'signals.py')
signals=importlib.util.module_from_spec(spec);spec.loader.exec_module(signals)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--interval',type=int,default=15)
    args=parser.parse_args()
    if not 2<=args.samples<=240 or not 1<=args.interval<=30:parser.error('Use 2–240 samples at 1–30 seconds')
    pod=signals.lab.POD
    result=subprocess.run(['podman','pod','inspect',pod],capture_output=True,text=True,check=True,timeout=10)
    if json.loads(result.stdout)[0]['Labels'].get('io.enmaas.test-lab')!=signals.lab.INSTANCE:
        raise ValueError('Unowned fixture')
    source=(HERE/'host-metrics.py').read_text()
    runner="import sys,json; scope={'__name__':'sample_only'}; exec(sys.stdin.read(),scope); print(json.dumps(scope['sample']()))"
    for index in range(args.samples):
        if Path('/proc/stat').exists():
            command=['python3','-c',runner]
        else:
            command=['podman','machine','ssh',shlex.join(['python3','-c',runner])]
        result=subprocess.run(command,input=source,capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError('Linux sampling failed; output suppressed')
        if len(result.stdout)>1024*1024:raise ValueError('Aggregate payload too large')
        signals.send('metrics',json.loads(result.stdout))
        print(f'Aggregate host sample {index+1}/{args.samples} delivered',flush=True)
        if index+1<args.samples:time.sleep(args.interval)

if __name__=='__main__':main()
