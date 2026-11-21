#!/usr/bin/env python3
"""Render anonymous per-stage database costs from validated benchmark evidence."""
import argparse
import json
import re
from pathlib import Path


LABELS={'role lookup by login','active identity lookup by login','identity policy lookup by login'}


def render(phases, labels=None):
    labels={} if labels is None else labels
    if not isinstance(labels,dict) or any(not re.fullmatch(r'-?[0-9]{1,20}',key) or value not in LABELS for key,value in labels.items()):
        raise ValueError('Unapproved diagnostic labels')
    lines=['# Database workload analysis', '',
        'Successful PostgreSQL executions in the metering database. Includes background activity during each stage; categories are heuristic, not proven API endpoints. Asynchronous work can cross stage boundaries, so statement counts are not a request-accounting reconciliation.', '',
        'Fingerprints identify statement shapes within this database. No SQL, parameters, users or credentials are included. Buffer reads are PostgreSQL buffer misses, not necessarily physical storage reads.', '',
        'Maximum execution time is since the statistics reset, not the stage maximum. These aggregates do not provide latency percentiles or time spent waiting for an application connection. Execution time is elapsed statement time, not CPU time; concurrent executions can sum to more than the stage duration.', '']
    lines += ['## Stage comparison', '',
        'Mean is weighted by completed statement calls, not request latency. Different operation mixes and background activity affect this value.', '',
        '| Stage | Statement calls | Aggregate execution (s) | Weighted mean (ms) |',
        '| --- | ---: | ---: | ---: |']
    for phase in phases:
        calls=sum(row['calls'] for row in phase['statements'])
        total=sum(row['total_ms'] for row in phase['statements'])
        mean=f'{total/calls:.3f}' if calls else '—'
        lines.append(f"| {phase['phase']} | {calls} | {total/1000:.3f} | {mean} |")
    lines += ['']
    for phase in phases:
        lines += ['## '+phase['phase'], '', '| Operation | Query fingerprint | Calls | Total execution (s) | Mean (ms) | Maximum since reset (ms) | Buffer hits | Buffer reads | Temp blocks written |',
                  '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
        for row in sorted(phase['statements'],key=lambda r:r['total_ms'],reverse=True)[:10]:
            lines.append('| '+ ' | '.join((labels.get(row['fingerprint'],row['operation']),'`'+row['fingerprint']+'`',str(row['calls']),f"{row['total_ms']/1000:.3f}",f"{row['mean_ms']:.3f}",f"{row['maximum_since_reset_ms']:.3f}",str(row['cache_hits']),str(row['disk_reads']),str(row['temporary_blocks'])))+' |')
        if not phase['statements']:lines += ['', 'No completed statements changed during this interval.']
        else:
            slow=sorted(phase['statements'],key=lambda r:r['mean_ms'],reverse=True)[:3]
            lines += ['', '**Highest mean execution time** (including rare calls): '+ '; '.join(f"`{r['fingerprint']}` ({r['operation']}): {r['mean_ms']:.3f} ms across {r['calls']} calls" for r in slow)+'.']
        lines += ['']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('run',type=Path)
    args=parser.parse_args()
    files=sorted(args.run.glob('stress_*-database.json'),key=lambda p:p.stat().st_mtime_ns)
    if not files:parser.error('No database profiles in this run')
    output=args.run/'database-analysis.md'
    label_path=args.run/'database-labels.json'
    labels=json.loads(label_path.read_text()) if label_path.exists() else {}
    output.write_text(render([json.loads(p.read_text()) for p in files],labels))
    output.chmod(0o600)
    print(output)

if __name__=='__main__':main()
