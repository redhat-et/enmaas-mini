#!/usr/bin/env python3
"""Sample the Podman Linux host and send aggregates into the local mTLS fixture."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import shlex
import time
import itertools
import local_probe

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('signals',HERE/'signals.py')
signals=importlib.util.module_from_spec(spec);spec.loader.exec_module(signals)


def collect():
    """Deliver one actual Linux host sample; callers own scheduling and bounds."""
    source=(HERE/'host-metrics.py').read_text()
    runner="import sys,json; scope={'__name__':'sample_only'}; exec(sys.stdin.read(),scope); print(json.dumps(scope['sample'](host_id='gateway')))"
    if Path('/proc/stat').exists():
        command=['python3','-c',runner]
    else:
        command=['podman','machine','ssh',shlex.join(['python3','-c',runner])]
    result=local_probe.read(command,input=source,text=True)
    if len(result.stdout)>1024*1024:raise ValueError('Aggregate payload too large')
    signals.send('metrics',json.loads(result.stdout))
    spec=importlib.util.spec_from_file_location('component_metrics',HERE/'container-metrics.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    listing=local_probe.read(['podman','ps','--filter','pod='+signals.lab.POD,'--format','{{.Names}}'],text=True)
    names=module.inventory(listing.stdout.splitlines(),signals.lab.POD)
    stats=local_probe.read(['podman','stats','--no-stream','--format','json',*names],text=True)
    rows=json.loads(stats.stdout)
    signals.send('metrics',module.payload(rows,signals.lab.POD,host_id="gateway"))
    spec=importlib.util.spec_from_file_location('database_metrics',HERE/'database-metrics.py')
    database=importlib.util.module_from_spec(spec);spec.loader.exec_module(database)
    signals.send('metrics',database.collect(signals.lab.POD,host_id="gateway"))
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--continuous',action='store_true',help='Keep monitoring until interrupted; does not generate benchmark traffic')
    parser.add_argument('--interval',type=int,default=15)
    args=parser.parse_args()
    if not 2<=args.samples<=240 or not 1<=args.interval<=30:parser.error('Use 2–240 samples at 1–30 seconds')
    pod=signals.lab.POD
    result=subprocess.run(['podman','pod','inspect',pod],capture_output=True,text=True,check=True,timeout=10)
    if json.loads(result.stdout)[0]['Labels'].get('io.enmaas.test-lab')!=signals.lab.INSTANCE:
        raise ValueError('Unowned fixture')
    for index in (itertools.count() if args.continuous else range(args.samples)):
        try:
            collect()
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
            if not args.continuous:raise
            print('Host sample unavailable: '+type(error).__name__,flush=True)
        else:
            print(f'Aggregate host sample {index+1} delivered',flush=True)
        if args.continuous or index+1<args.samples:time.sleep(args.interval)

if __name__=='__main__':main()
