"""Aggregate PostgreSQL telemetry without query text, identities or credentials."""
import json
import math
import re
import subprocess
import time

# Current database only. No application rows, SQL text or connection addresses.
QUERY = """SELECT json_build_object(
 'connections',numbackends,
 'active_connections',(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active' AND pid<>pg_backend_pid()),
 'lock_waiters',(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'),
 'connection_limit',current_setting('max_connections')::int,
 'transactions_committed',xact_commit,'transactions_rolled_back',xact_rollback,
 'deadlocks',deadlocks,'temp_bytes',temp_bytes,
 'size_bytes',pg_database_size(current_database()))
 FROM pg_stat_database WHERE datname=current_database()"""
COUNTERS={'transactions_committed','transactions_rolled_back','deadlocks','temp_bytes'}
FIELDS=COUNTERS|{'connections','active_connections','lock_waiters','connection_limit','size_bytes'}


def payload(values,host_id='gateway',now=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}',host_id):raise ValueError('Invalid host alias')
    if not isinstance(values,dict) or set(values)!=FIELDS:raise ValueError('Incomplete database telemetry')
    timestamp=str(time.time_ns() if now is None else now)
    metrics=[]
    for name in sorted(FIELDS):
        value=values[name]
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Invalid database measurement')
        data={'dataPoints':[{'timeUnixNano':timestamp,'asDouble':value,'attributes':[
          {'key':'host','value':{'stringValue':host_id}},
          {'key':'component','value':{'stringValue':'db'}}]}]}
        if name in COUNTERS:data.update(aggregationTemporality=2,isMonotonic=True)
        metrics.append({'name':'enmaas_postgres_'+name,'sum' if name in COUNTERS else 'gauge':data})
    return {'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-postgres-sampler'}}]},'scopeMetrics':[{'metrics':metrics}]}]}


def collect(pod,host_id='gateway'):
    if not re.fullmatch(r'enmaas-[a-z0-9-]{1,50}',pod):raise ValueError('Invalid local pod alias')
    # This local fixture role already exists. Never expose a database port or password.
    result=subprocess.run(['podman','exec',pod+'-db','psql','-X','-U','keycloak','-d','metering_sso','-At',
      '-c',"BEGIN READ ONLY; SET LOCAL statement_timeout='3s'; "+QUERY+'; COMMIT;'],
      capture_output=True,text=True,timeout=8,check=True)
    if len(result.stdout)>8192:raise ValueError('Oversized database telemetry')
    rows=[line for line in result.stdout.splitlines() if line.startswith('{')]
    if len(rows)!=1:raise ValueError('Missing database telemetry')
    return payload(json.loads(rows[0]),host_id)
