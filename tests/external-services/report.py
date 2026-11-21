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
        host=row.get('host','')
        if host and (not isinstance(host,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}',host)):
            raise ValueError('Invalid host alias')
        if not isinstance(row['containers'],list):raise ValueError('Resource collection failed')
        for container in row['containers']:
            name=container['name']
            primary,separator,replica=name.partition('--')
            if separator and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}',replica):
                raise ValueError('Invalid service instance alias')
            role=next((role for role in ('gateway-sso','metering-sso','gateway-relay','gateway','metering','maas','provider','db','keycloak','auth','perses','prometheus','loki','tempo','collector')
                       if primary.endswith('-'+role)),None)
            if role is None:raise ValueError('Unexpected measured service')
            role=role.removesuffix('-sso')
            role=' / '.join(part for part in (host,role,replica) if part)
            identifier=(host,container['id'])
            counter=cpu_seconds(container['cpu_time'])
            cpu=float('nan') # No interval exists for a new/restarted container.
            if identifier in previous:
                old_at,old_counter=previous[identifier]
                if at<=old_at:raise ValueError('CPU sample time went backwards')
                # Podman can reset a stopped/restarted container's counter without changing its ID.
                # Preserve a visible gap; never invent negative or zero interval CPU.
                if counter>=old_counter:cpu=100*(counter-old_counter)/(at-old_at)
            previous[identifier]=(at,counter)
            memory=memory_bytes(container['mem_usage'].split('/')[0])/1024**2
            series.setdefault(role,[]).append((at,cpu,memory))
    if not series:raise ValueError('No resource samples')
    for points in series.values():
        points.sort()
        if len(points)<2:raise ValueError('Need at least two samples per measured service')
    return series


def host_resources(records):
    """Keep absent observations as gaps; local fixtures may have none of these."""
    metrics={'available_gib':('memory','MemAvailable_kib',1024**2),
        'cached_gib':('memory','Cached_kib',1024**2),
        'disk_percent':(None,'disk_available_percent',1),
        'database_mib':('postgres','database_bytes',1024**2),
        'wal_mib':('postgres','wal_directory_bytes',1024**2)}
    series={key:[] for key in metrics}
    for row in sorted(records,key=lambda row:stamp(row['utc'])):
        at=stamp(row['utc'])
        for name,(parent,field,divisor) in metrics.items():
            source=row.get(parent,{}) if parent else row
            if not isinstance(source,dict):raise ValueError('Invalid host/storage sample')
            raw=source.get(field)
            value=float('nan') if raw is None else number(raw)/divisor
            if name=='disk_percent' and value>100:raise ValueError('Invalid free-disk percentage')
            series[name].append((at,value))
    return {name:points for name,points in series.items() if any(math.isfinite(y) for _,y in points)}


def phase_shading(axes,phases,start):
    for index,phase in enumerate(phases):
        left=(stamp(phase['started_utc'])-start)/60;right=(stamp(phase['ended_utc'])-start)/60
        for ax in axes:ax.axvspan(left,right,alpha=.06,color='blue' if index%2 else 'gray')
        axes[0].text((left+right)/2,1.02,f'P{index+1:02d}',transform=axes[0].get_xaxis_transform(),ha='center',fontsize=7,rotation=90)


def host_graph(plt,directory,records,phases,start):
    series=host_resources(records)
    panels=[('Host memory (GiB)',(('available_gib','Available'),('cached_gib','Cached'))),
            ('Free filesystem (%)',(('disk_percent','/var available'),)),
            ('PostgreSQL storage (MiB)',(('database_mib','Database'),('wal_mib','WAL directory')))]
    panels=[(label,[(key,name) for key,name in entries if key in series]) for label,entries in panels]
    panels=[(label,entries) for label,entries in panels if entries]
    if not panels:return False
    fig,axes=plt.subplots(len(panels),1,figsize=(12,3*len(panels)),sharex=True,layout='constrained',squeeze=False)
    axes=list(axes[:,0]);phase_shading(axes,phases,start)
    for ax,(label,entries) in zip(axes,panels):
        for key,name in entries:
            points=series[key]
            ax.plot([(at-start)/60 for at,_ in points],[value for _,value in points],marker='.',label=name)
        ax.set_ylabel(label);ax.set_ylim(bottom=0);ax.grid(alpha=.2);ax.legend(loc='upper left')
        if label=='Free filesystem (%)':ax.set_ylim(0,100)
    axes[-1].set_xlabel('Minutes since first workload phase')
    fig.savefig(directory/'host-storage.png',dpi=150);plt.close(fig)
    return True



def capacity_table(data):
    columns=(('host_cpu_percent','maximum'),('host_available_memory_percent','minimum'),
             ('host_available_disk_percent','minimum'),('sampled_containers_memory_mib','maximum'),
             ('metering_pool_wait_seconds_per_second','maximum'),('mean_inference_seconds','maximum'))
    rows=[]
    for phase in data['phases']:
        values=[phase['phase']+' / '+str(phase['cycle'])]
        values.extend(f"{number(phase['metrics'][metric][statistic]):.2f}" for metric,statistic in columns)
        rows.append('<tr>'+''.join('<td>'+html.escape(value)+'</td>' for value in values)+'</tr>')
    warning=('<p role="alert"><strong>Qualification failed:</strong> '+html.escape(str(data.get('failure','See run results')))+'. These measurements are diagnostic evidence, not a passing capacity result.</p>' if data.get('qualification_passed') is False else '')
    return (warning+'<h2>Measured headroom by phase</h2><p>'+html.escape(data['scope'])+
      '. Values use 15-second query steps; rate metrics use two-minute windows, which overlap phase boundaries. '
      'Container memory totals are sampled together, not the sum of independent per-container peaks. '
      'Maximum mean latency is not a percentile.</p><table><thead><tr><th>Phase / cycle</th>'
      '<th>Peak host CPU %</th><th>Minimum available memory %</th><th>Minimum free disk %</th>'
      '<th>Peak sampled container MiB</th><th>Peak pool wait s/s</th><th>Maximum mean inference s</th>'
      '</tr></thead><tbody>'+''.join(rows)+'</tbody></table>')


def stress_tick(index,phase):
    rate='Recovery' if phase['name'].split(' / ')[-1]=='stress_recovery' else str(phase['offered_rps'])+'/s'
    return f'P{index+1:02d}\n'+rate


def stress_graph(plt,directory,phases):
    if not all('offered_slots' in p and 'dispatched' in p for p in phases):return False
    x=list(range(len(phases)))
    offered=[number(p['offered_rps']) for p in phases]
    dispatched=[number(p['dispatched'])/max(number(p['admission_seconds']),1e-9) for p in phases]
    completed=[number(p['completed_rps']) for p in phases]
    successful=[number(p['statuses'].get('200',0))/max(number(p['elapsed_seconds']),1e-9) for p in phases]
    fig,axes=plt.subplots(3,1,figsize=(12,9),sharex=True,layout='constrained')
    for values,label,color in ((offered,'Offered target','tab:blue'),(dispatched,'Dispatched / admission time','tab:orange'),(completed,'All outcomes / time including drain','tab:gray'),(successful,'HTTP 200 / time including drain','tab:green')):
        axes[0].plot(x,values,marker='o',label=label,color=color)
    axes[0].set_ylabel('Requests / second')
    if max(offered)>100:axes[0].set_yscale('symlog',linthresh=1)
    axes[0].set_ylim(bottom=0)
    axes[1].plot(x,[number(p['peak_in_flight']) for p in phases],marker='o',label='Observed peak')
    axes[1].plot(x,[number(p['max_in_flight']) for p in phases],linestyle='--',label='Generator cap')
    axes[1].set_ylabel('Concurrent requests');axes[1].set_ylim(bottom=0)
    for key,label in (('skipped_concurrency','Concurrency cap'),('skipped_scheduler','Scheduler delay')):
        axes[2].plot(x,[100*number(p[key])/max(number(p['offered_slots']),1) for p in phases],marker='o',label=label)
    axes[2].set_ylabel('Offered admissions skipped (%)');axes[2].set_ylim(0,100)
    axes[2].set_xticks(x,[stress_tick(i,p) for i,p in enumerate(phases)],fontsize=8)
    axes[2].set_xlabel('Phase ID / offered requests per second; IDs match the results table')
    for ax in axes:ax.grid(alpha=.2);ax.legend(loc='upper left',fontsize=8)
    axes[0].legend(loc='lower center',bbox_to_anchor=(.5,1.01),ncols=2,fontsize=8)
    fig.savefig(directory/'staircase.png',dpi=150);plt.close(fig)
    return True


def failure_notice(summary):
    if not summary.get('failure_type'):return ''
    detail=' — '.join(str(summary.get(key,'')) for key in ('failure_phase','failure_type','failure_reason') if summary.get(key))
    return '<h2>Run failed — not qualified</h2><p>Last workload phase and recorded failure: '+html.escape(detail)+'</p><p>Only completed stages are tabulated. A partial stage is excluded; failure can also occur afterward during accounting verification or cleanup. Inspect summary.json, generator-latest.json and any failure-services.json for the recorded evidence.</p>'


def render(directory):
    os.environ.setdefault('MPLCONFIGDIR',str(directory/'.matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    summary=json.loads(read(directory/'summary.json'))
    if summary.get('mock_only') is not True:raise ValueError('Require mock benchmark summary')
    source=directory/'resources.jsonl'
    if not source.exists():source=directory/'observe.jsonl'
    records=[json.loads(line) for line in read(source).splitlines() if line.strip()]
    data=resources(records)
    phases=summary['phases']
    if not phases:raise ValueError('No completed benchmark phases')
    start=min(stamp(phase['started_utc']) for phase in phases)
    fig,axes=plt.subplots(2,1,figsize=(12,7),sharex=True,layout='constrained')
    palette=plt.get_cmap('tab20')
    for index,(role,points) in enumerate(sorted(data.items())):
        color=palette(index)
        x=[(point[0]-start)/60 for point in points]
        axes[0].plot(x,[p[1]/100 for p in points],marker='.',label=role,color=color)
        axes[1].plot(x,[p[2] for p in points],marker='.',label=role,color=color)
    phase_shading(axes,phases,start)
    axes[0].set_ylabel('CPU cores used')
    axes[1].set_ylabel('Container memory (MiB)');axes[1].set_xlabel('Minutes since first workload phase')
    for ax in axes:
        ax.set_ylim(bottom=0);ax.grid(alpha=.2);ax.legend(loc='upper left',ncols=5,fontsize=8)
    fig.savefig(directory/'resources.png',dpi=150);plt.close(fig)
    has_host=host_graph(plt,directory,records,phases,start)
    has_stress=stress_graph(plt,directory,phases)
    rows=[]
    for index,phase in enumerate(phases):
        concurrency=(str(phase['streams_per_user'])+' per user' if 'streams_per_user' in phase else str(phase['max_in_flight'])+' shared cap')
        values=[f"P{index+1:02d} — {phase['name']} / cycle {phase.get('cycle',1)}",phase['users'],concurrency,phase['target_seconds'],
                phase['attempts'],phase['statuses'].get('200',0),phase['statuses'].get('429',0),
                f"{number(phase['completed_rps']):.2f}",
                number(phase['peak_in_flight']) if 'peak_in_flight' in phase else 'not measured',
                f"{number(phase['mean_in_flight']):.2f}" if 'mean_in_flight' in phase else 'not measured']
        rows.append('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in values)+'</tr>')
    cleanup=json.loads(read(directory/'cleanup.json')) if (directory/'cleanup.json').exists() else {}
    status=f"Ledger verified: {summary.get('ledger_verified') is True}. Cleanup complete: {cleanup.get('complete') is True}."
    document='''<!doctype html><html lang="en"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'">
<title>Mock gateway benchmark</title><style>body{font:16px system-ui;margin:2rem auto;max-width:1200px;padding:0 1rem}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #ccc}img{max-width:100%}p{line-height:1.5}</style>
<h1>Mock gateway benchmark</h1>'''+failure_notice(summary)+'''<p>'''+html.escape(status)+'''</p>
<p>Deterministic mock providers. Connection policy is recorded in the run summary; asynchronous runs use a bounded HTTPS connection pool. These measurements describe this run, not a production capacity guarantee. Component CPU is measured in CPU seconds per elapsed second: one fully busy vCPU equals one core. The separate host CPU percentage uses all vCPUs as 100%. New containers have no CPU point until a second sample; restarts break the series. Memory is the container statistic, not Linux host availability. Shading marks active phase intervals; short phase IDs match the results table. Gaps include restarts and sampling.</p>
<table><thead><tr><th>Phase</th><th>Users</th><th>Concurrency configuration</th><th>Target seconds</th><th>Attempts</th><th>HTTP 200</th><th>HTTP 429</th><th>Completed requests/s</th><th>Observed peak in flight</th><th>Observed mean in flight</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<h2>Service resources over time</h2><img src="resources.png" alt="Container CPU and memory measurements across workload phases"></html>'''
    if has_stress:
        document=document.replace('</html>','<h2>Offered load and generator limits</h2><p>Dispatch rate uses admission time; all-outcome and HTTP 200 rates include request draining. All outcomes include errors; HTTP 200 shows successful responses. Skipped admissions never reached the gateway. A skipped stage is not an achieved target.</p><img src="staircase.png" alt="Offered, dispatched and completed traffic with concurrency and skipped admissions"></html>')
    if (directory/'capacity-summary.json').exists():
        document=document.replace('</html>',capacity_table(json.loads(read(directory/'capacity-summary.json')))+'</html>')
    people=summary.get('synthetic_personas',{})
    if people:
        rows=[]
        for person in people.values():
            values=[person.get(k,'') for k in ('name','account_type','country','team','model','monthly_usd')]
            rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>')
        section='<h2>Synthetic people and service accounts</h2><p>Simulated clients and directory locations. These are not real people or source-IP geolocation. Budget values are monthly USD limits.</p><table><thead><tr><th>Name</th><th>Account</th><th>Country</th><th>Team</th><th>Model</th><th>Monthly USD</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'
        document=document.replace('</html>',section+'</html>')
    if has_host:
        section='<h2>Host headroom and storage</h2><p>Available memory includes reclaimable capacity; cached memory overlaps it and must not be added to it. Database and WAL measurements cover the selected database and PostgreSQL cluster, respectively, not only this workload. Gaps mean unavailable samples. Growth does not prove retention or backup behavior.</p><img src="host-storage.png" alt="Host memory, free disk and PostgreSQL storage measurements">'
        document=document.replace('</html>',section+'</html>')
    (directory/'report.html').write_text(document)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path,help='Private benchmark output directory')
    args=parser.parse_args();os.umask(0o077)
    if not args.directory.is_dir():parser.error('Benchmark directory does not exist')
    render(args.directory)
    print('Offline report: '+str(args.directory/'report.html'))

if __name__=='__main__':main()
