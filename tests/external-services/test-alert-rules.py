#!/usr/bin/env python3
"""Evaluate production alert rules with isolated, deterministic time-series fixtures."""
import importlib.util
import json
from pathlib import Path
import subprocess
import signal
import sys
import uuid
import yaml

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('monitor',HERE/'monitoring.py')
monitor=importlib.util.module_from_spec(spec);spec.loader.exec_module(monitor)


def expected(name,extra=None):
    rule=next(rule for rule in monitor.rules()['groups'][0]['rules'] if rule['alert']=='Enmaas'+name)
    return {'exp_labels':dict(rule['labels'],**(extra or {})),'exp_annotations':rule['annotations']}


def suite():
    tests=[]
    def case(name,series,checks):
        tests.append({'name':name,'interval':'15s','input_series':series,'alert_rule_test':checks})
    def check(name,time,firing,extra=None):
        return {'eval_time':time,'alertname':'Enmaas'+name,'exp_alerts':[expected(name,extra)] if firing else []}
    case('memory pressure holds before firing and recovers',[
      {'series':'enmaas_host_memory_bytes{state="total"}','values':'100+0x40'},
      {'series':'enmaas_host_memory_bytes{state="available"}','values':'5+0x15 50+0x25'},
    ],[check('HostMemoryCritical','15s',False),check('HostMemoryCritical','45s',True),
       check('HostMemoryLow','1m',False),check('HostMemoryLow','3m',True),
       check('HostMemoryCritical','5m',False),check('HostMemoryLow','5m',False)])
    case('healthy capacity produces no memory warnings',[
      {'series':'enmaas_host_memory_bytes{state="total"}','values':'100+0x40'},
      {'series':'enmaas_host_memory_bytes{state="available"}','values':'60+0x40'},
    ],[check('HostMemoryCritical','5m',False),check('HostMemoryLow','5m',False)])
    case('cached host samples are detected as stale',[
      {'series':'enmaas_host_sample_unixtime_seconds{host="gateway"}','values':'0+0x40'},
    ],[check('HostTelemetryStale','1m',False),check('HostTelemetryStale','3m',True,{'host':'gateway'})])
    case('fresh host samples stay healthy',[
      {'series':'enmaas_host_sample_unixtime_seconds','values':'0+15x40'},
    ],[check('HostTelemetryStale','5m',False)])
    case('policy rejections fire and then recover',[
      {'series':'praxis_rate_limit_limited_total','values':'0+5x16 80+0x24'},
    ],[check('RateLimitActive','2m',True),check('RateLimitActive','8m',False)])
    def sample(name,values):return {'series':name,'values':values}
    for name,metric in (('GatewayMetricsStale','praxis_process_open_fds'),('AccountingMetricsStale','metering_db_pool_open_conns')):
        case(name+' remains firing when cached samples age out',[
          sample(metric,' '.join(['10']+['_']*80)),
        ],[check(name,'3m',True),check(name,'5m30s',True),check(name,'7m',True)])
    positive=[
      ('HostCpuHigh',[sample('enmaas_host_cpu_seconds_total{state="user"}','0+90x40'),sample('enmaas_host_cpu_seconds_total{state="idle"}','0+5x40')],{}),
      ('HostDiskLow',[sample('enmaas_host_disk_bytes{state="total"}','100+0x40'),sample('enmaas_host_disk_bytes{state="available"}','10+0x40')],{}),
      ('HostDiskCritical',[sample('enmaas_host_disk_bytes{state="total"}','100+0x40'),sample('enmaas_host_disk_bytes{state="available"}','5+0x40')],{}),
      ('ContainerMemoryHigh',[sample('enmaas_container_memory_bytes{component="gateway-sso"}','90+0x40'),sample('enmaas_container_memory_limit_bytes{component="gateway-sso"}','100+0x40')],{'component':'gateway-sso'}),
      ('FileDescriptorsHigh',[sample('praxis_process_open_fds','90+0x40'),sample('praxis_process_max_fds','100+0x40')],{}),
      ('DatabasePoolWaiting',[sample('metering_db_pool_wait_seconds_total','0+3x40')],{}),
      ('AccountingInsertErrors',[sample('metering_usage_event_insert_errors_total','0+1x40')],{}),
      ('InferenceSlow',[sample('praxis_http_request_duration_seconds_sum{cluster="authenticated-openai"}','0+90x40'),sample('praxis_http_request_duration_seconds_count{cluster="authenticated-openai"}','0+10x40')],{}),
      ('GatewayServerErrors',[sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="5xx"}','0+10x40'),sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="2xx"}','0+40x40')],{}),
      ('QuotaDenials',[sample('metering_quota_denials_total','0+1x40')],{}),
      ('GatewayScrapeFailed',[sample('up{exported_job="praxis"}','0+0x40')],{'exported_job':'praxis'}),
      ('AccountingScrapeFailed',[sample('up{exported_job="pricetag-metering"}','0+0x40')],{'exported_job':'pricetag-metering'}),
      ('GatewayMetricsStale',[sample('praxis_process_open_fds',' '.join(['10']+['_']*40))],{}),
      ('AccountingMetricsStale',[sample('metering_db_pool_open_conns',' '.join(['10']+['_']*40))],{}),
      ('GatewayScrapeMissing',[],{}),('AccountingScrapeMissing',[],{}),
    ]
    for name,series,extra in positive:
        case(name+' fires on its actual metric contract',series,[{'eval_time':'4m','alertname':'Enmaas'+name,'exp_alerts':[expected(name,extra)]}])
    healthy=[
      sample('enmaas_host_cpu_seconds_total{state="user"}','0+10x40'),sample('enmaas_host_cpu_seconds_total{state="idle"}','0+90x40'),
      sample('enmaas_host_memory_bytes{state="total"}','100+0x40'),sample('enmaas_host_memory_bytes{state="available"}','60+0x40'),
      sample('enmaas_host_disk_bytes{state="total"}','100+0x40'),sample('enmaas_host_disk_bytes{state="available"}','50+0x40'),
      sample('enmaas_container_memory_bytes{component="gateway-sso"}','10+0x40'),sample('enmaas_container_memory_limit_bytes{component="gateway-sso"}','100+0x40'),
      sample('praxis_process_open_fds','10+0x40'),sample('praxis_process_max_fds','100+0x40'),
      sample('metering_db_pool_open_conns','10+0x40'),sample('metering_db_pool_wait_seconds_total','0+0x40'),
      sample('metering_usage_event_insert_errors_total','0+0x40'),
      sample('praxis_http_request_duration_seconds_sum{cluster="authenticated-openai"}','0+20x40'),sample('praxis_http_request_duration_seconds_count{cluster="authenticated-openai"}','0+10x40'),
      sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="5xx"}','0+0x40'),sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="2xx"}','0+10x40'),
      sample('praxis_rate_limit_limited_total','0+0x40'),sample('metering_quota_denials_total','0+0x40'),
      sample('enmaas_host_sample_unixtime_seconds','0+15x40'),
      sample('up{exported_job="praxis"}','1+0x40'),sample('up{exported_job="pricetag-metering"}','1+0x40')]
    case('healthy stack has no false firing alerts',healthy,[check(rule[0],'5m',False) for rule in monitor.RULES])
    for cores in (4,8):
        tests.append({'name':f'component CPU remains two cores on a {cores}-vCPU host','interval':'15s',
          'input_series':[
            sample('enmaas_host_cpu_seconds_total{state="user"}',f'0+{cores*7.5}x20'),
            sample('enmaas_host_cpu_seconds_total{state="idle"}',f'0+{cores*7.5}x20'),
            sample('enmaas_container_cpu_seconds_total{component="gateway-sso"}','0+30x20')],
          'promql_expr_test':[{'expr':monitor.COMPONENT_CPU,'eval_time':'4m',
            'exp_samples':[{'labels':'{component="gateway-sso"}','value':2}]}]})
    case('one overloaded host is not averaged with an idle host',[
      sample('enmaas_host_cpu_seconds_total{host="hot",state="user"}','0+95x40'),
      sample('enmaas_host_cpu_seconds_total{host="hot",state="idle"}','0+5x40'),
      sample('enmaas_host_cpu_seconds_total{host="cool",state="user"}','0+5x40'),
      sample('enmaas_host_cpu_seconds_total{host="cool",state="idle"}','0+95x40')],
      [check('HostCpuHigh','4m',True,{'host':'hot'})])
    tests.append({'name':'same service on two hosts retains separate CPU series','interval':'15s',
      'input_series':[
        sample('enmaas_container_cpu_seconds_total{host="one",component="gateway-sso"}','0+30x20'),
        sample('enmaas_container_cpu_seconds_total{host="two",component="gateway-sso"}','0+15x20')],
      'promql_expr_test':[{'expr':monitor.COMPONENT_CPU,'eval_time':'4m','exp_samples':[
        {'labels':'{host="one",component="gateway-sso"}','value':2},
        {'labels':'{host="two",component="gateway-sso"}','value':1}]}]})
    panels=monitor.dashboards()['monitoring-dashboard.json']['spec']['panels']
    def expression(key):return panels[key]['spec']['queries'][0]['spec']['plugin']['spec']['query'].replace('$host','.+').replace('$response_class','(2xx|1xx|unknown)')
    for fresh in (True,False):
        tests.append({'name':'memory overview '+('fresh' if fresh else 'stale is unavailable'),'interval':'15s',
          'input_series':[sample('enmaas_host_memory_bytes{host="gateway",state="total"}','100+0x40'),
                          sample('enmaas_host_memory_bytes{host="gateway",state="available"}','60+0x40'),
                          sample('enmaas_host_sample_unixtime_seconds{host="gateway"}','0+15x40' if fresh else '0+0x40')],
          'promql_expr_test':[{'expr':expression('currentmemory'),'eval_time':'4m',
            'exp_samples':[{'labels':'{host="gateway"}','value':40}] if fresh else []}]})
    tests.append({'name':'main throughput excludes separately displayed errors and internal hops','interval':'15s',
      'input_series':[sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="2xx"}','0+10x40'),
                      sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="5xx"}','0+1x40'),
                      sample('praxis_http_requests_total{cluster="private-internal",status_class="2xx"}','0+100x40')],
      'promql_expr_test':[{'expr':expression('gatewaytraffic'),'eval_time':'4m','exp_samples':[
        {'labels':'{cluster="authenticated-openai",status_class="2xx"}','value':10/15}]}]})
    tests.append({'name':'missing public traffic is not manufactured as zero','interval':'15s',
      'input_series':[sample('praxis_process_open_fds','10+0x40')],
      'promql_expr_test':[{'expr':expression('gatewaytraffic'),'eval_time':'4m','exp_samples':[]}]})
    for status in ('3xx','4xx','5xx'):
        for available in (True,False):
            tests.append({'name':status+' zero requires observed public counters','interval':'15s',
              'input_series':[sample('praxis_process_open_fds','10+0x40')]+([sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="2xx"}','0+10x40')] if available else []),
              'promql_expr_test':[{'expr':expression('public'+status),'eval_time':'4m','exp_samples':[{'labels':'{}','value':0}] if available else []}]})
        tests.append({'name':status+' reports its own response rate','interval':'15s',
          'input_series':[sample('praxis_process_open_fds','10+0x40'),sample('praxis_http_requests_total{cluster="authenticated-openai",status_class="'+status+'"}','0+15x40')],
          'promql_expr_test':[{'expr':expression('public'+status),'eval_time':'4m','exp_samples':[{'labels':'{}','value':1}]}]})
    return {'rule_files':['/rules.yaml'],'evaluation_interval':'15s','fuzzy_compare':True,'tests':tests}


def main(*, ruleset=None, cases=None):
    image=json.loads((HERE/'images.json').read_text())['prometheus']['image']
    prefix='enmaas-rule-test-'+uuid.uuid4().hex[:10]
    names=[]
    try:
        mounts=[]
        for suffix,content in [('rules',yaml.safe_dump(monitor.rules() if ruleset is None else ruleset,sort_keys=False)),
                               ('tests',yaml.safe_dump(suite() if cases is None else cases))]:
            name=prefix+'-'+suffix
            subprocess.run(['podman','secret','create',name,'-'],input=content.encode(),check=True,stdout=subprocess.DEVNULL,timeout=30)
            names.append(name);mounts+=['--secret',name+',target=/'+suffix+'.yaml,mode=0444']
        subprocess.run(['podman','run','--rm','--pull=never','--name',prefix,'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
          '--memory','256m','--pids-limit','64',*mounts,'--entrypoint','/bin/promtool',image,'test','rules','/tests.yaml'],check=True,timeout=120)
    finally:
        cleanup(prefix,names)


def cleanup(prefix,names):
    failed=False
    for operation in [('rm','--force','--ignore',prefix),*[('secret','rm',name) for name in reversed(names)]]:
        try:
            result=subprocess.run(['podman',*operation],capture_output=True,timeout=10)
            failed |= result.returncode!=0
        except (OSError,subprocess.TimeoutExpired):failed=True
    if failed:raise RuntimeError('Rule test cleanup incomplete; inspect '+prefix)

if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    main()
