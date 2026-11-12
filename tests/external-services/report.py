#!/usr/bin/env python3
"""Render a private, offline benchmark report from local or RHEL runner output."""
import argparse
import datetime
import html
import json
import math
import os
from pathlib import Path
import re

MAX_BYTES=32*1024*1024


def read(path):
    with path.open() as stream:data=stream.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:raise ValueError('Report input exceeds 32 MiB')
    return data


def number(value):
    value=float(value)
    if not math.isfinite(value) or value<0:raise ValueError('Invalid resource value')
    return value


def memory_bytes(value):
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)',value.strip())
    if not match:raise ValueError('Unknown Podman memory unit')
    units={'B':1,'kB':1000,'KB':1000,'MB':10**6,'GB':10**9,'TB':10**12,
           'KiB':1024,'MiB':1024**2,'GiB':1024**3,'TiB':1024**4}
    return number(match[1])*units[match[2]]


def cpu_seconds(value):
    parts=re.findall(r'([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)',value)
    if not parts or ''.join(n+unit for n,unit in parts)!=value:raise ValueError('Unknown CPU duration')
    units={'ns':1e-9,'us':1e-6,'µs':1e-6,'ms':.001,'s':1,'m':60,'h':3600}
    return sum(number(n)*units[unit] for n,unit in parts)


def stamp(value):
    parsed=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('Resource timestamp must include timezone')
    return parsed.timestamp()


def resources(records):
    series={};previous={}
    for row in sorted(records,key=lambda row:stamp(row['utc'])):
        at=stamp(row['utc'])
        if not isinstance(row['containers'],list):raise ValueError('Resource collection failed')
        for container in row['containers']:
            name=container['name']
            role=next((role for role in ('gateway-sso','metering-sso','gateway','metering','maas','provider','db')
                       if name.endswith('-'+role)),None)
            if role is None:raise ValueError('Unexpected measured service')
            role=role.removesuffix('-sso')
            identifier=container['id']
            counter=cpu_seconds(container['cpu_time'])
            cpu=float('nan') # No interval exists for a new/restarted container.
            if identifier in previous:
                old_at,old_counter=previous[identifier]
                if at<=old_at or counter<old_counter:raise ValueError('CPU counter/time went backwards')
                cpu=100*(counter-old_counter)/(at-old_at)
            previous[identifier]=(at,counter)
            memory=memory_bytes(container['mem_usage'].split('/')[0])/1024**2
            series.setdefault(role,[]).append((at,cpu,memory))
    if not series:raise ValueError('No resource samples')
    for points in series.values():
        points.sort()
        if len(points)<2:raise ValueError('Need at least two samples per measured service')
    return series


def render(directory):
    os.environ.setdefault('MPLCONFIGDIR',str(directory/'.matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    summary=json.loads(read(directory/'summary.json'))
    if summary.get('mock_only') is not True:raise ValueError('Require mock benchmark summary')
    source=directory/'resources.jsonl'
    if not source.exists():source=directory/'observe.jsonl'
    data=resources([json.loads(line) for line in read(source).splitlines() if line.strip()])
    phases=summary['phases']
    if not phases:raise ValueError('No completed benchmark phases')
    start=min(stamp(phase['started_utc']) for phase in phases)
    fig,axes=plt.subplots(2,1,figsize=(12,7),sharex=True,layout='constrained')
    for role,points in sorted(data.items()):
        x=[(point[0]-start)/60 for point in points]
        axes[0].plot(x,[p[1] for p in points],marker='.',label=role)
        axes[1].plot(x,[p[2] for p in points],marker='.',label=role)
    for index,phase in enumerate(phases):
        left=(stamp(phase['started_utc'])-start)/60;right=(stamp(phase['ended_utc'])-start)/60
        for ax in axes:ax.axvspan(left,right,alpha=.06,color='blue' if index%2 else 'gray')
        axes[0].text((left+right)/2,1.02,str(phase['name']),transform=axes[0].get_xaxis_transform(),ha='center',fontsize=8)
    axes[0].set_ylabel('Container CPU (%)')
    axes[1].set_ylabel('Container memory (MiB)');axes[1].set_xlabel('Minutes since first workload phase')
    for ax in axes:
        ax.set_ylim(bottom=0);ax.grid(alpha=.2);ax.legend(loc='upper left',ncols=5,fontsize=8)
    fig.savefig(directory/'resources.png',dpi=150);plt.close(fig)
    rows=[]
    for phase in phases:
        values=[phase['name'],phase['users'],phase['streams_per_user'],phase['target_seconds'],
                phase['attempts'],phase['statuses'].get('200',0),phase['statuses'].get('429',0),
                f"{number(phase['completed_rps']):.2f}"]
        rows.append('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in values)+'</tr>')
    cleanup=json.loads(read(directory/'cleanup.json')) if (directory/'cleanup.json').exists() else {}
    status=f"Ledger verified: {summary.get('ledger_verified') is True}. Cleanup complete: {cleanup.get('complete') is True}."
    document='''<!doctype html><html lang="en"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'">
<title>Mock gateway benchmark</title><style>body{font:16px system-ui;margin:2rem auto;max-width:1200px;padding:0 1rem}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #ccc}img{max-width:100%}p{line-height:1.5}</style>
<h1>Mock gateway benchmark</h1><p>'''+html.escape(status)+'''</p>
<p>Deterministic mock providers; new TLS connection per request. These measurements describe this run, not a production capacity guarantee. CPU is derived from changes in cumulative container CPU time between samples and may exceed 100% on multiple cores. New containers have no CPU point until a second sample; restarts break the series. Memory is the container statistic, not Linux host availability. Shading marks active phase intervals; gaps include restarts and sampling.</p>
<table><thead><tr><th>Phase</th><th>Users</th><th>Maximum in flight per user</th><th>Target seconds</th><th>Attempts</th><th>HTTP 200</th><th>HTTP 429</th><th>Completed requests/s</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<h2>Service resources over time</h2><img src="resources.png" alt="Container CPU and memory measurements across workload phases"></html>'''
    (directory/'report.html').write_text(document)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path,help='Private benchmark output directory')
    args=parser.parse_args();os.umask(0o077)
    if not args.directory.is_dir():parser.error('Benchmark directory does not exist')
    render(args.directory)
    print('Offline report: '+str(args.directory/'report.html'))

if __name__=='__main__':main()
