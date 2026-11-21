#!/usr/bin/env python3
"""Plot sampled disposable Keycloak resources; never infer login capacity."""
import argparse
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from report import cpu_seconds, memory_bytes, number, read


def series(rows):
    previous=None;points=[];identity=None
    for row in rows:
        at=number(row['time'])
        if len(row['samples'])!=1:raise ValueError('Require exactly one issuer per sample')
        item=row['samples'][0]
        if not item['name'].startswith('enmaas-issuer-path-') or item['name'].endswith('-proxy'):
            raise ValueError('Unexpected measured container')
        if identity is None:identity=item['id']
        if item['id']!=identity:raise ValueError('Issuer changed during measurement')
        counter=cpu_seconds(item['cpu_time'])
        used,limit=(memory_bytes(part) for part in item['mem_usage'].split('/'))
        if limit<=0 or used>limit:raise ValueError('Invalid memory bounds')
        cpu=None
        if previous is not None:
            old_at,old_counter=previous
            if at<=old_at or counter<old_counter:raise ValueError('Counter or timestamp went backwards')
            cpu=100*(counter-old_counter)/(at-old_at)
        points.append({'time':at,'cpu_percent':cpu,'memory_mib':used/1024**2,'limit_mib':limit/1024**2})
        previous=at,counter
    if len(points)<2:raise ValueError('At least two samples required')
    return points


def render(source,output):
    points=series([json.loads(line) for line in read(source).splitlines() if line.strip()])
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    os.environ.setdefault('MPLCONFIGDIR',str(output/'.matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    start=points[0]['time'];x=[(p['time']-start)/60 for p in points]
    fig,axes=plt.subplots(2,1,figsize=(10,6),sharex=True,layout='constrained')
    axes[0].plot(x,[p['cpu_percent'] if p['cpu_percent'] is not None else math.nan for p in points])
    axes[0].set_ylabel('CPU % (100% = one core)')
    axes[1].plot(x,[p['memory_mib'] for p in points],label='Sampled usage')
    axes[1].plot(x,[p['limit_mib'] for p in points],linestyle='--',label='Container limit')
    axes[1].set_ylabel('Memory (MiB)');axes[1].legend()
    axes[1].set_xlabel('Minutes since first sample')
    fig.suptitle('Disposable Keycloak — issuer provisioning resources')
    for axis in axes:axis.grid(alpha=.25)
    fig.savefig(output/'resources.png',dpi=160);plt.close(fig)
    summary={'samples':len(points),'sample_window_seconds':points[-1]['time']-start,
        'peak_sampled_memory_mib':max(p['memory_mib'] for p in points),
        'peak_interval_cpu_percent':max(p['cpu_percent'] for p in points[1:]),
        'scope':'Local development database; sampled window only, not concurrent login or RHEL capacity'}
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('Resource report:',output/'resources.png')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    render(args.samples,args.output)
