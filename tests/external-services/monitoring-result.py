#!/usr/bin/env python3
"""Check an owned local run's accounting, actual rate-limit alert and recovery evidence."""
import argparse
import datetime
import json
import math
import monitoring
import benchmark_monitoring
from pathlib import Path
import time
import urllib.parse
import signals


def stamp(value):return datetime.datetime.fromisoformat(value).timestamp()


def check_report(summary,cleanup):
    if summary.get('mock_only') is not True or summary.get('ledger_verified') is not True or cleanup.get('complete') is not True:
        raise ValueError('Requires a reconciled mock run with completed cleanup')
    phases=summary.get('phases',[])
    expected=['baseline','team','parallel','sustained_high','rate_limited','recovery']
    if not phases or len(phases)%len(expected):raise ValueError('Incomplete sustained sequence')
    for index,phase in enumerate(phases):
        if phase.get('name')!=expected[index%len(expected)] or phase.get('cycle')!=index//len(expected)+1:
            raise ValueError('Sustained phases or cycle order do not match the plan')
        if phase.get('interrupted') or phase.get('errors') or phase.get('telemetry_verified') is not True:
            raise ValueError('Interrupted or unqualified phase')
        if phase['admission_seconds']<phase['target_seconds']-.5:raise ValueError('Phase did not sustain its configured duration')
        statuses=phase.get('statuses',{})
        allowed={'200','429'} if phase['name']=='rate_limited' else {'200'}
        if (not isinstance(statuses,dict) or not set(statuses)<=allowed or
                any(type(value) is not int or value<0 for value in statuses.values()) or statuses.get('200',0)<1):
            raise ValueError('Unexpected HTTP status or missing successful inference')
        if phase['name']=='rate_limited' and statuses.get('429',0)<1:raise ValueError('Missing intentional rate-limit rejection')
    return phases



def check_rules(reply):
    if not reply or reply.get('status')!='success':
        raise RuntimeError('Monitoring rule query failed')
    groups=reply.get('data',{}).get('groups',[])
    definitions=benchmark_monitoring.rules()['groups']
    names={group['name'] for group in definitions}
    selected=[group for group in groups if group.get('name') in names]
    if len(selected)!=len(names) or {group.get('name') for group in selected}!=names:
        raise RuntimeError('Operational or benchmark alert group is missing or duplicated')
    active=[rule for group in selected for rule in group.get('rules',[])]
    expected={rule['alert'] for group in definitions for rule in group['rules']}
    if len(active)!=len(expected) or {rule.get('name') for rule in active}!=expected:
        raise RuntimeError('Monitoring alert rule set is incomplete or unexpected')
    if any(rule.get('health')!='ok' or rule.get('lastError') for rule in active):
        raise RuntimeError('Monitoring rule evaluation is unhealthy')
    return active


def measurements(reply):
    if not reply or reply.get('status')!='success':raise ValueError('Resource query failed')
    rows=reply.get('data',{}).get('result',[])
    if len(rows)!=1:raise ValueError('Expected one aggregate resource series')
    values=[float(point[1]) for point in rows[0].get('values',[]) if math.isfinite(float(point[1]))]
    if not values:raise ValueError('No finite resource measurements')
    return {'minimum':min(values),'maximum':max(values),'mean':sum(values)/len(values),'samples':len(values)}


def series_measurements(reply):
    """Preserve resource identity; never flatten replicas into one average."""
    if not reply or reply.get('status')!='success':raise ValueError('Resource query failed')
    output=[]
    for row in reply.get('data',{}).get('result',[]):
        labels={key:value for key,value in row.get('metric',{}).items() if key in ('host','component','replica')}
        values=[float(point[1]) for point in row.get('values',[]) if math.isfinite(float(point[1]))]
        if not values:continue
        output.append({'labels':labels,'minimum':min(values),'maximum':max(values),'mean':sum(values)/len(values),'samples':len(values)})
    if not output:raise ValueError('No finite per-resource measurements')
    return output


def capacity(phases):
    queries={
      'host_cpu_percent':'max('+monitoring.CPU+')',
      'host_available_memory_percent':'min('+monitoring.MEM+')',
      'host_available_disk_percent':'min('+monitoring.DISK+')',
      'sampled_containers_memory_mib':'sum(enmaas_container_memory_bytes and on(host) (time() - enmaas_host_sample_unixtime_seconds{host!=""} < 90)) / 1024 / 1024',
      'metering_pool_wait_seconds_per_second':'sum(rate(metering_db_pool_wait_seconds_total[2m]))',
      'mean_inference_seconds':monitoring.MEAN,
    }
    results=[]
    for phase in phases:
        result={'phase':phase['name'],'cycle':phase['cycle'],'metrics':{}}
        for name,query in queries.items():
            reply=signals.query('http://localhost:9090/api/v1/query_range?'+urllib.parse.urlencode({
              'query':query,'start':stamp(phase['started_utc']),'end':stamp(phase['ended_utc']),'step':15}))
            result['metrics'][name]=measurements(reply)
        result['resources']={}
        for name,query in {
          'host_cpu_percent':monitoring.CPU,
          'host_available_memory_percent':monitoring.MEM,
          'host_available_disk_percent':monitoring.DISK,
          'service_cpu_cores':'rate(enmaas_container_cpu_seconds_total[2m])',
          'service_memory_bytes':'enmaas_container_memory_bytes',
          'database_size_bytes':'enmaas_postgres_size_bytes',
          'database_connections':'enmaas_postgres_connections',
          'database_lock_waiters':'enmaas_postgres_lock_waiters',
        }.items():
            reply=signals.query('http://localhost:9090/api/v1/query_range?'+urllib.parse.urlencode({
              'query':query,'start':stamp(phase['started_utc']),'end':stamp(phase['ended_utc']),'step':15}))
            result['resources'][name]=series_measurements(reply)
        results.append(result)
    scope=('Linux Podman host samples; service totals include only explicitly sampled containers. '
           'Pod workers consume this host; a host-side generator/browser can run outside the VM. '
           'Use the run summary for generator placement; worker limits/exits are recorded separately.')
    return {'scope':scope,
      'summary_host_policy':'Worst host at each sample; per-resource measurements retain host/service/replica labels',
      'rate_window_seconds':120,'sample_step_seconds':15,'phases':results}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('report',type=Path);args=parser.parse_args()
    root=signals.lab.STATE.resolve();report=args.report.resolve()
    if report.parent!=root or not report.name.startswith('lab-run-'):raise ValueError('Report must belong to the selected local fixture')
    summary=json.loads((report/'summary.json').read_text());cleanup=json.loads((report/'cleanup.json').read_text())
    phases=check_report(summary,cleanup)
    expression='ALERTS{alertname="EnmaasRateLimitActive",alertstate="firing"}'
    for phase in phases:
        if phase['name']!='rate_limited':continue
        reply=signals.query('http://localhost:9090/api/v1/query_range?'+urllib.parse.urlencode({
          'query':expression,'start':stamp(phase['started_utc']),'end':stamp(phase['ended_utc']),'step':5})) or {}
        if not any(float(point[1])==1 for row in reply.get('data',{}).get('result',[]) for point in row.get('values',[])):
            raise RuntimeError('No actual policy alert during the rate-limited phase')
    for _ in range(60):
        reply=signals.query('http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({'query':expression}))
        if reply and reply.get('status')=='success' and not reply.get('data',{}).get('result'):break
        time.sleep(3)
    else:raise RuntimeError('Rate-limit alert did not recover')
    rules=signals.query('http://localhost:9090/api/v1/rules') or {}
    active=check_rules(rules)
    result={'mock_only':True,'phase_count':len(phases),'sustained_seconds':sum(p['admission_seconds'] for p in phases),
      'successful_requests':sum(p['statuses'].get('200',0) for p in phases),
      'expected_rate_denials':sum(p['statuses'].get('429',0) for p in phases),
      'peak_concurrency':max(p['peak_in_flight'] for p in phases),'ledger_verified':True,'cleanup_complete':True,
      'policy_alert_fired':True,'policy_alert_recovered':True,'healthy_rules':len(active),
      'dashboard_url':signals.lab.url('observability','/projects/praxis/dashboards/enmaas-monitoring?')+urllib.parse.urlencode({
        'start':int(stamp(phases[0]['started_utc'])*1000),'end':int(stamp(phases[-1]['ended_utc'])*1000),'refresh':'0s'})}
    signals.lab.write(report,'capacity-summary.json',capacity(phases))
    signals.lab.write(report,'monitoring-summary.json',result)
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
