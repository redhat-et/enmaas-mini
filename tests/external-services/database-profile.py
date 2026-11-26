"""Bounded anonymized statement aggregates; raw SQL never leaves PostgreSQL."""
import json
import math
import re
import subprocess

OPERATIONS={'usage-read','usage-write','pricing-read','identity-read','read','write','other'}
FIELDS={'fingerprint','operation','calls','total_ms','maximum_ms','rows','cache_hits','disk_reads','temporary_blocks'}
QUERY="""WITH classified AS (
 SELECT queryid,
 CASE
  WHEN query ~* '^[[:space:]]*(insert|update|delete)' AND query ~* 'usage_events' THEN 'usage-write'
  WHEN query ~* 'usage_events|usage_hourly' THEN 'usage-read'
  WHEN query ~* 'model_pricing' THEN 'pricing-read'
  WHEN query ~* 'identities' THEN 'identity-read'
  WHEN query ~* '^[[:space:]]*(select|with)' THEN 'read'
  WHEN query ~* '^[[:space:]]*(insert|update|delete)' THEN 'write'
  ELSE 'other' END AS operation,
 calls,total_exec_time,max_exec_time,rows,shared_blks_hit,shared_blks_read,temp_blks_written
 FROM pg_stat_statements
 WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
 AND queryid IS NOT NULL AND calls>0
 AND query !~* 'pg_stat_statements'
 AND query ~* '^[[:space:]]*(select|with|insert|update|delete)'
), aggregates AS (
 SELECT queryid::text AS fingerprint, operation, sum(calls) AS calls,
 sum(total_exec_time) AS total_ms,max(max_exec_time) AS maximum_ms,sum(rows) AS rows,
 sum(shared_blks_hit) AS cache_hits,sum(shared_blks_read) AS disk_reads,
 sum(temp_blks_written) AS temporary_blocks
 FROM classified GROUP BY queryid,operation ORDER BY sum(total_exec_time) DESC LIMIT 1001
) SELECT coalesce(json_agg(aggregates),'[]'::json) FROM aggregates"""


def validate(rows):
    if not isinstance(rows,list) or len(rows)>1000:raise ValueError('Statement profile exceeds bounded coverage')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!=FIELDS:raise ValueError('Unexpected statement profile fields')
        key=row['fingerprint']
        if not isinstance(key,str) or not re.fullmatch(r'-?[0-9]{1,20}',key) or key in seen:raise ValueError('Invalid statement fingerprint')
        seen.add(key)
        if row['operation'] not in OPERATIONS:raise ValueError('Unapproved statement category')
        for name in FIELDS-{'fingerprint','operation'}:
            value=row[name]
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Invalid statement measurement')
    return rows


def delta(before,after):
    """Difference cumulative costs; maximum is explicitly lifetime, not phase-local."""
    old={row['fingerprint']:row for row in validate(before)}
    current=validate(after)
    if set(old)-{row['fingerprint'] for row in current}:
        raise ValueError('Statement statistics evicted during measured interval')
    output=[]
    for row in current:
        prior=old.get(row['fingerprint'],{})
        if prior and prior['operation']!=row['operation']:
            raise ValueError('Statement category changed during measured interval')
        item={key:row[key] for key in ('fingerprint','operation')}
        for key in FIELDS-{'fingerprint','operation','maximum_ms'}:
            item[key]=row[key]-prior.get(key,0)
            if item[key]<0:raise ValueError('Statement statistics reset during measured interval')
        if not item['calls']:continue
        item['mean_ms']=item['total_ms']/item['calls']
        item['maximum_since_reset_ms']=row['maximum_ms']
        output.append(item)
    return sorted(output,key=lambda row:row['total_ms'],reverse=True)


def collect(pod):
    if not re.fullmatch(r'enmaas-[a-z0-9-]{1,50}',pod):raise ValueError('Invalid owned pod alias')
    result=subprocess.run(['podman','exec',pod+'-db','psql','-X','-U','keycloak','-d','metering_sso','-At','-q','-v','ON_ERROR_STOP=1',
      '-c',"BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; "+QUERY+'; COMMIT;'],
      capture_output=True,text=True,timeout=8,check=True)
    if len(result.stdout)>1024*1024:raise ValueError('Statement profile too large')
    return validate(json.loads(result.stdout))


def compare(before, after):
    """Reject interval attribution after global resets or statement eviction."""
    for snapshot in (before, after):
        if set(snapshot) != {'stats_reset', 'dealloc', 'rows'}:
            raise ValueError('Unexpected snapshot fields')
        if not isinstance(snapshot['stats_reset'], str) or not snapshot['stats_reset']:
            raise ValueError('Missing statistics epoch')
        if type(snapshot['dealloc']) is not int or snapshot['dealloc'] < 0:
            raise ValueError('Invalid eviction counter')
    if any(before[key] != after[key] for key in ('stats_reset', 'dealloc')):
        raise ValueError('Statement statistics reset or evicted during interval')
    return delta(before['rows'], after['rows'])


def snapshot(pod):
    """Bracket collection so a reset during the collection itself is rejected."""
    if not re.fullmatch(r'enmaas-[a-z0-9-]{1,50}', pod):
        raise ValueError('Invalid owned pod alias')
    def epoch():
        result = subprocess.run(['podman', 'exec', pod+'-db', 'psql', '-X', '-U',
            'keycloak', '-d', 'metering_sso', '-At', '-v', 'ON_ERROR_STOP=1', '-c',
            "SELECT json_build_object('stats_reset',stats_reset,'dealloc',dealloc) FROM pg_stat_statements_info"],
            capture_output=True, text=True, timeout=8, check=True)
        if len(result.stdout) > 1024:
            raise ValueError('Unexpected statistics metadata')
        return json.loads(result.stdout)
    start = epoch()
    rows = collect(pod)
    end = epoch()
    if start != end:
        raise ValueError('Statement statistics changed during snapshot')
    output = dict(end, rows=rows)
    compare(output, output)
    return output
