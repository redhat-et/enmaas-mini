"""Operational dashboards and alerts; benchmark definitions are separate."""
from pathlib import Path
import json
import re
import yaml

HERE=Path(__file__).resolve().parent
BASE='/projects/praxis/dashboards/'
PUBLIC='cluster=~"authenticated-openai|authenticated-anthropic|dashboard|none"'
INFERENCE='cluster=~"authenticated-openai|authenticated-anthropic"'
CPU='100 * sum by(host)(rate(enmaas_host_cpu_seconds_total{state!~"idle|iowait"}[2m])) / clamp_min(sum by(host)(rate(enmaas_host_cpu_seconds_total[2m])), 1e-9)'
COMPONENT_CPU='rate(enmaas_container_cpu_seconds_total[2m])'
MEM='100 * sum by(host)(enmaas_host_memory_bytes{state="available"}) / sum by(host)(enmaas_host_memory_bytes{state="total"})'
DISK='100 * sum by(host)(enmaas_host_disk_bytes{state="available"}) / sum by(host)(enmaas_host_disk_bytes{state="total"})'
MEAN=f'sum(rate(praxis_http_request_duration_seconds_sum{{{INFERENCE}}}[2m])) / sum(rate(praxis_http_request_duration_seconds_count{{{INFERENCE}}}[2m]))'
RULES=[
 ('HostCpuHigh','capacity','warning',CPU+' > 85','2m','Host CPU over 85%','Reduce offered load; inspect host CPU','host'),
 ('HostMemoryLow','capacity','warning',MEM+' < 15','2m','Host available memory below 15%','Reduce concurrency; inspect memory and cache','host'),
 ('HostMemoryCritical','capacity','critical',MEM+' < 8','30s','Host available memory below 8%','Stop load before OOM','host'),
 ('HostDiskLow','storage','warning',DISK+' < 15','2m','Data filesystem below 15% available','Inspect retention and stored data','host'),
 ('HostDiskCritical','storage','critical',DISK+' < 8','30s','Data filesystem below 8% available','Stop load; reclaim owned retained data','host'),
 ('ContainerMemoryHigh','capacity','warning','100 * enmaas_container_memory_bytes / (enmaas_container_memory_limit_bytes > 0) > 80','1m','Component memory above 80% of its limit','Inspect component headroom before increasing load','components'),
 ('FileDescriptorsHigh','capacity','warning','100 * praxis_process_open_fds / praxis_process_max_fds > 80','2m','Gateway file descriptors above 80%','Inspect concurrency and upstream connections','gateway'),
 ('DatabasePoolWaiting','usage_metering','warning','sum(rate(metering_db_pool_wait_seconds_total[2m])) > 0.1','2m','Metering database pool is waiting','Inspect pool use and database latency','accounting'),
 ('AccountingInsertErrors','usage_metering','critical','sum(increase(metering_usage_event_insert_errors_total[2m])) > 0','15s','Usage insertion errors observed','Stop capacity claims; investigate usage-record delivery','accounting'),
 ('InferenceSlow','traffic','warning',MEAN+' > 5','2m','Mean inference duration above five seconds','Compare upstream delay with CPU and pool waits','gateway'),
 ('GatewayServerErrors','traffic','critical',f'sum(rate(praxis_http_requests_total{{{PUBLIC},status_class="5xx"}}[2m])) / clamp_min(sum(rate(praxis_http_requests_total{{{PUBLIC}}}[2m])), 1e-9) > 0.05','1m','More than 5% gateway responses are 5xx','Inspect traces and upstream failures','gateway'),
 ('RateLimitActive','policy','warning','sum(rate(praxis_rate_limit_limited_total[2m])) > 0','15s','Rate limiting is rejecting traffic','Expected in denial phase; not proof of capacity exhaustion','gateway'),
 ('QuotaDenials','policy','warning','sum(rate(metering_quota_denials_total[2m])) > 0','15s','Spending policy denied requests','Inspect PriceTag budgets; not a host bottleneck','accounting'),
 ('GatewayScrapeFailed','telemetry','critical','up{exported_job="praxis"} == 0','1m','Gateway scrape failed','Inspect the gateway metrics listener and relay','gateway'),
 ('AccountingScrapeFailed','telemetry','critical','up{exported_job="pricetag-metering"} == 0','1m','PriceTag scrape failed','Inspect the metering metrics listener and relay','accounting'),
 ('HostTelemetryStale','telemetry','critical','(time() - max_over_time(enmaas_host_sample_unixtime_seconds{host!=""}[24h]) > 90) or absent(enmaas_host_sample_unixtime_seconds)','1m','Host samples absent or stale','Start sampler; missing telemetry is not healthy capacity','host'),
 ('GatewayMetricsStale','telemetry','critical','(time() - max(timestamp(praxis_process_open_fds)) > 30) or absent(praxis_process_open_fds)','1m','Gateway metric samples are stale','Inspect the relay and receiving collector','gateway'),
 ('AccountingMetricsStale','telemetry','critical','(time() - max(timestamp(metering_db_pool_open_conns)) > 30) or absent(metering_db_pool_open_conns)','1m','PriceTag metric samples are stale','Inspect the relay and receiving collector','accounting'),
 ('GatewayScrapeMissing','telemetry','critical','absent(praxis_process_open_fds)','1m','Gateway metrics absent','Inspect gateway relay and scrape target','gateway'),
 ('AccountingScrapeMissing','telemetry','critical','absent(metering_db_pool_open_conns)','1m','PriceTag metrics absent','Inspect metering relay and scrape target','accounting'),
]


def rules():
    return {'groups':[{'name':'enmaas-operational','interval':'15s','rules':[
      {'alert':'Enmaas'+name,'expr':expr,'for':duration,
       'labels':{'severity':severity,'category':category,'summary':summary,'action':action,'detail':detail,
                 **({'component':{'host':'linux-host','gateway':'praxis','accounting':'pricetag-metering'}.get(detail,detail)} if name!='ContainerMemoryHigh' else {})},
       'annotations':{'summary':summary,'description':action,'dashboard':BASE+'enmaas-'+detail}}
      for name,category,severity,expr,duration,summary,action,detail in RULES]}]}


def link(name,target):return {'name':name,'url':BASE+'enmaas-'+target,'targetBlank':False}

def status_logs_link(status):
    if status not in ('3xx','4xx','5xx'):
        raise ValueError('Unsupported status class')
    return {'name':'View '+status+' logs',
            'url':BASE+'enmaas-logs?var-log_service=%28enmaas-gateway.%2A%7Cpricetag-gateway%29&var-log_status='+status[0]+'%5B0-9%5D%5B0-9%5D'+
                  '&start=${__from}&end=${__to}&refresh=0s', 'targetBlank':False}


def log_filters(document):
    """Only retained structured status fields are queried; never parse raw bodies."""
    variables=[]
    for name,title,default,values in (
        ('log_service','Service','.+',[('.+','All services'),('(enmaas-gateway.*|pricetag-gateway)','Gateway')]),
        ('log_status','HTTP status','.*',[('.*','All / unclassified')]+[(str(i)+'[0-9][0-9]',str(i)+'xx') for i in (3,4,5)]),
    ):
        variables.append({'kind':'ListVariable','spec':{'name':name,'display':{'name':title},
            'defaultValue':default,'allowMultiple':False,'allowAllValue':False,
            'plugin':{'kind':'StaticListVariable','spec':{'values':[{'value':value,'label':label} for value,label in values]}}}})
    document['spec']['variables']=variables
    panel=document['spec']['panels']['0']['spec']
    panel['queries'][0]['spec']['plugin']['spec']['query']='{service_name=~"$log_service"} | http_response_status_code=~"$log_status"'
    panel['display']['description']='Sanitized events only. HTTP filters require a retained structured status code. An empty filtered result means no matching collected events, not zero gateway errors; compare gateway metrics and sampled traces. Original messages, headers and request bodies are not exposed.'
    return document


def chart(title,query,detail,description='',kind='TimeSeriesChart',instant=False,unit='decimal'):
    if description:
        description='MEANING\n'+description+'\n\nTIME WINDOW\n'+('Snapshot at the selected end time.' if instant else 'Measurements over the selected time window; gaps mean no matching samples.')
    series=('{{category}} · {{summary}} — {{action}}' if query.startswith('ALERTS') else
            'Host: {{host}} · Service: {{component}} · Instance: {{replica}}' if 'enmaas_container_' in query else
            '{{host}}' if 'enmaas_host_' in query or 'enmaas_postgres_' in query else
            '{{cluster}} · {{status_class}}' if 'by(cluster,status_class)' in query else
            '{{cluster}}' if 'by(cluster)' in query else
            '{{__name__}}' if query.startswith('{__name__') else title)
    options={}
    if kind=='TimeSeriesChart':options={'legend':{'mode':'table','position':'bottom','values':['max']},'visual':{'areaOpacity':0,'lineWidth':2},'yAxis':{'format':{'unit':unit,'decimalPlaces':2,**({'shortValues':True} if unit in ('bytes','decimal') else {})}}}
    elif kind=='StatChart':options={'calculation':'last-number','format':{'unit':'decimal','decimalPlaces':0},
        'thresholds':{'mode':'absolute','defaultColor':'#52606d','steps':[{'value':1,'color':'#b71c1c' if 'critical' in title else '#995200'}]}}
    elif kind=='Table':
        options={'density':'standard','pagination':True,'defaultColumnHeight':'auto','columnSettings':[
          {'name':'category','header':'Category','sort':'asc','width':110},
          {'name':'severity','header':'Severity','width':100},
          {'name':'summary','header':'Condition','width':260},
          {'name':'host','header':'Host','width':120},
          {'name':'component','header':'Service','width':130},
          {'name':'replica','header':'Instance','width':100},
          {'name':'action','header':'Response','width':330},
          {'name':'detail','header':'Details','width':100,'dataLink':{'url':BASE+'enmaas-${__data.fields["detail"]}','openNewTab':False}},
          *[{'name':name,'hide':True} for name in ('timestamp','value','__name__','alertname','alertstate','exported_job','instance','job','otel_scope_name')]],
          'cellSettings':[{'condition':{'kind':'Value','spec':{'value':'critical'}},'textColor':'#b71c1c'},
                          {'condition':{'kind':'Value','spec':{'value':'warning'}},'textColor':'#995200'}]}
    return {'kind':'Panel','spec':{'display':{'name':title,'description':description},
      'plugin':{'kind':kind,'spec':options},
      'queries':[{'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':query,'instant':instant,'seriesNameFormat':series}}}}],
      'links':[link('Details',detail)]}}


def resource_chart(title,query,detail,description,used,total,absolute_unit,labels=('Used','Total')):
    panel=chart(title,query,detail,description+' Left axis: %. Right axis and hover: absolute used/total. Legend checkboxes toggle individual series.',unit='percent')
    spec=panel['spec'];spec['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='Used capacity'
    for expression,label in zip((used,total),labels):
        spec['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{
            'query':expression,'instant':False,'seriesNameFormat':label}}}})
    options=spec['plugin']['spec']
    options['querySettings']=[{'queryIndex':index,'format':{'unit':absolute_unit,'decimalPlaces':2,
       **({'shortValues':True} if absolute_unit in ('bytes','decimal') else {})},'lineStyle':'dashed'} for index in (1,2)]
    options['tooltip']={'enablePinning':True}
    options['yAxis'].update(min=0,max=100)
    # Installed Perses formats legend aggregates with the primary query unit.
    # Keep series toggles; show correctly formatted absolute values in the hover.
    options['legend']['values']=[]
    return panel


def section(title,text):
    return {'kind':'Panel','spec':{'display':{'name':title},'plugin':{'kind':'Markdown','spec':{'text':text}}}}


def current(title,query,detail,description,unit,freshness,thresholds=None):
    description += '\n\nTime: selected dashboard end time. Missing or stale data is unavailable, not zero.'
    if thresholds:
        description += '\n\nColors: green below warning; orange at warning; red at critical. Alert rules may require a sustained duration.'
    panel=chart(title,'('+query+') and on() ('+freshness+')',detail,description,'StatChart',True)
    options={'calculation':'last-number','format':{'unit':unit,'decimalPlaces':1,**({'shortValues':True} if unit in ('bytes','decimal') else {})},
             'thresholds':{'mode':'absolute','defaultColor':'#52606d','steps':[]}}
    if thresholds:
        warning,critical=thresholds
        options['thresholds']={'mode':'absolute','defaultColor':'#52606d','steps':[
            {'value':0,'color':'#2e7d32'}, {'value':warning,'color':'#ed6c02'}, {'value':critical,'color':'#d32f2f'}]}
    panel['spec']['plugin']['spec']=options
    return panel


def overview_sources():
    gatewayfresh='time() - max(timestamp(praxis_process_open_fds)) < 30'
    accountingfresh='time() - max(timestamp(metering_db_pool_open_conns)) < 30'
    hostfresh='time() - max(enmaas_host_sample_unixtime_seconds) < 90'
    return {
        'currentcpu':current('Host CPU used',CPU,'host','Busy CPU divided by total host CPU capacity; all vCPUs together equal 100%. Warning 85%, critical 95%.','percent',hostfresh,(85,95)),
        'currentmemory':current('Host memory pressure','100 - ('+MEM+')','host','Total minus MemAvailable, divided by total RAM. Reclaimable cache is available capacity. Warning 85%, critical 92%.','percent',hostfresh,(85,92)),
        'currenttraffic':current('Public throughput',f'sum(rate(praxis_http_requests_total{{{PUBLIC}}}[2m]))','gateway','Completed public responses per second, including policy denials and errors.','requests/sec',gatewayfresh),
        'currentactive':current('Active public requests','sum(praxis_http_active_requests{listener="https"})','gateway','Concurrent requests at the public listener; no universal service concurrency limit is assumed.','decimal',gatewayfresh),
        'currentlatency':current('Mean inference duration',MEAN,'gateway','Completed inference duration includes provider and streaming time; compare with provider response time and resource pressure.','seconds',gatewayfresh),
        'currentdb':current('Database pool wait (s/s)','sum(rate(metering_db_pool_wait_seconds_total[2m]))','accounting','Aggregate wait seconds per elapsed second. Multiple waiters can exceed 1 s/s; not a backlog count.','decimal',accountingfresh),
        'accountingfailures':current('Usage recording errors — last 2 minutes','sum(increase(metering_usage_event_insert_errors_total[2m]))','accounting','Any positive count requires investigation. Zero does not replace exact ledger reconciliation.','decimal',accountingfresh,(0.01,0.02)),
        'dbsessions':current('Database sessions','sum(enmaas_postgres_connections)','accounting','Metering database connections across monitored sources. Compare with server limits in Database & usage metering.','decimal','time() - max(timestamp(enmaas_postgres_connections)) < 90'),
    }


def production_overview(source):
    """Operational overview: host-scoped resources and explicitly fleet-wide traffic."""
    import copy
    panels=[('intro',section('Main monitoring',
        '**Host** selects resource measurements below; All keeps one series per host or service instance. '
        '**Gateway traffic and usage metering are fleet-wide**: their current exported metrics do not carry the host mapping. '
        'Do not infer a host from the collector address. Missing measurements are unavailable. '
        '[Database & usage metering]('+BASE+'enmaas-accounting) · [Gateway detail]('+BASE+'enmaas-gateway).'),24,3),
      ('gatewaysection',section('Gateway — traffic and outcomes',
        'Public responses are counted once. Provider calls are internal hops and must not be added to public requests. '
        'Duration includes streaming and provider time; it is not proxy overhead or time to first token.'),24,2)]
    overview=[]
    for key,expression in (('currentcpu',CPU),('currentmemory','100 - ('+MEM+')')):
        panel=copy.deepcopy(source[key])
        panel['spec']['queries'][0]['spec']['plugin']['spec']['query']='('+expression+') and on(host) (time() - enmaas_host_sample_unixtime_seconds{host!=""} < 90)'
        panel['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='{{host}}'
        panel['spec']['display']['description'] += '\n\nSCOPE\nOne value per selected host; never a sum of percentages. Select one host for a compact view. Color bands describe pressure; alert rules have their own duration thresholds.'
        overview.append((key,panel,6,3))
    for severity in ('critical','warning'):
        overview.append((severity+'count',chart('Firing '+severity+' alerts','count(ALERTS{alertstate="firing",category!="generator",severity="'+severity+'",alertname=~"Enmaas.*"}) or vector(0)','alerts','Fleet-wide operational alerts; excludes benchmark generator alerts. A zero count means no firing alerts, not proof that every collector is healthy.','StatChart',True),6,3))
    panels[1:1]=overview
    for key in ('currenttraffic','currentactive','currentlatency','currentdb'):
        panel=copy.deepcopy(source[key])
        panels.append((key,panel,6,3))
    for status,label,meaning in (
      ('3xx','redirects','Redirect responses; not automatically a failure.'),
      ('4xx','client and policy rejections','Includes authentication, authorization, quota and rate-limit denials; expected policy enforcement is not a server failure.'),
      ('5xx','server errors','Failures reported by the public gateway; inspect upstream responses and traces to identify the cause.'),
    ):
        query=(f'(sum(rate(praxis_http_requests_total{{{PUBLIC},status_class="{status}"}}[2m])) '
               f'or (0 * sum(rate(praxis_http_requests_total{{{PUBLIC}}}[2m])))) '
               'and on() (time() - max(timestamp(praxis_process_open_fds)) < 90)')
        panel=chart(status+' — '+label,query,'gateway',meaning+' Fleet-wide public responses per second over two minutes. Zero requires observed public counters and fresh gateway telemetry; absent telemetry remains unavailable.',unit='requests/sec')
        panel['spec']['links'].append(link('Logs','logs'))
        panel['spec']['links'].append(status_logs_link(status))
        panels.append(('public'+status,panel,8,8))
    for key,title,query,unit,description in (
      ('gatewaytraffic','Public throughput — selected response classes',f'sum by(cluster,status_class)(rate(praxis_http_requests_total{{{PUBLIC},status_class=~"$response_class"}}[2m]))','requests/sec','HTTP 200 is included in 2xx alongside other successful status codes. Use Response classes to add redirects or errors. Defaults show 2xx, informational 1xx and unclassified responses. Redirects, client/policy rejections and server errors have separate panels. Fleet-wide public responses only; excludes internal hops.'),
      ('upstream','Internal upstream responses by route and status','sum by(cluster,status_class)(rate(praxis_upstream_requests_total[2m]))','requests/sec','Fleet-wide internal calls, including provider and management routes. Inspect failures here without counting them as additional users.'),
      ('active','Active public requests over time','praxis_http_active_requests{listener="https"}','decimal','Concurrent public requests, retaining available exporter identity. Source identities are not a verified host mapping.'),
      ('latency','Mean inference duration by route',f'sum by(cluster)(rate(praxis_http_request_duration_seconds_sum{{{INFERENCE}}}[2m])) / sum by(cluster)(rate(praxis_http_request_duration_seconds_count{{{INFERENCE}}}[2m]))','seconds','Mean completed stream duration per inference route. No traffic means undefined. Histograms and first-token timing are not currently exported.'),
      ('fds','Gateway file descriptor utilization','100 * praxis_process_open_fds / praxis_process_max_fds','percent','Open file descriptors divided by each process limit. Preserve exporter identity; do not sum percentages. Warning starts at 80%.'),
    ):
        panel=chart(title,query,'gateway',description,unit=unit)
        if key in ('active','fds'):
            panel['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='{{exported_job}}'
        if key=='gatewaytraffic':panel['spec']['links'].append(link('Logs','logs'))
        if key in ('gatewaytraffic','upstream'):
            panel['spec']['plugin']['spec']['legend']={'mode':'table','position':'right','size':'medium','values':['max']}
        panels.append((key,panel,24 if key in ('gatewaytraffic','upstream') else 12,8))
    panels.append(('hostsection',section('Hosts — selected hosts, separate series',
       'Each host has its own 100% CPU and memory capacity. Select fewer hosts for a readable comparison. '
       'Memory uses MemAvailable, including reclaimable cache; disk is filesystem capacity, not I/O utilization. '
       'CPU warning: 85%; memory pressure: 85%; disk capacity used: 85%.'),24,3))
    for key,title,query in (('hostcpu','CPU utilization by host',CPU),('hostmemory','Memory pressure by host','100 - ('+MEM+')'),('hostdisk','Disk capacity used by host','100 - ('+DISK+')')):
        panel=chart(title,'('+query+') and on(host) (time() - enmaas_host_sample_unixtime_seconds{host!=""} < 90)','host','One independently normalized series per selected host. Stale samples are excluded; inspect telemetry age when a host disappears.',unit='percent')
        panel['spec']['plugin']['spec']['yAxis'].update(min=0,max=100)
        panels.append((key,panel,24,8))
    panels.append(('gatewayresources',section('Gateway — resources on selected hosts',
        'CPU is in cores; memory is in MiB/GiB. Separate series identify host, service and instance. '
        'Resource ceilings belong to individual containers, not to the whole deployment. '
        '[All service resources]('+BASE+'enmaas-components)'),24,2))
    for key,title,query,unit in (
      ('gatewaycpu','Gateway CPU — cores','rate(enmaas_container_cpu_seconds_total{component="gateway-sso"}[2m])','decimal'),
      ('gatewaymemory','Gateway memory','enmaas_container_memory_bytes{component="gateway-sso"}','bytes'),
      ('gatewaylimit','Gateway memory limit','enmaas_container_memory_limit_bytes{component="gateway-sso"}','bytes'),
      ('health','Host telemetry age','time() - enmaas_host_sample_unixtime_seconds{host!=""}','seconds')):
        panels.append((key,chart(title,query,'components' if key!='health' else 'host','Per selected host/instance. No inferred capacity or missing-data zero; telemetry age above 90 seconds is stale.',unit=unit),12,8))
    panels.append(('controlresources',section('MaaS Standalone and PriceTag Metering — service resources',
        'CPU is measured in cores; memory and its configured ceiling use MiB/GiB. '
        'Separate lines retain host and instance identity. A gap is missing observation, not zero use or proof of health. '
        '[All service resources]('+BASE+'enmaas-components) · [Database and usage processing]('+BASE+'enmaas-accounting)'),24,3))
    for key,name,component in (('maas','MaaS Standalone','maas'),('metering','PriceTag Metering','metering-sso')):
        selector='{component="'+component+'"}'
        cpu=chart(name+' CPU — cores','rate(enmaas_container_cpu_seconds_total'+selector+'[2m])','components',
            name+' CPU time per second over two minutes, separately per selected host and instance. One busy core is 1. Compare the timeline with request load; CPU alone does not prove API health.',unit='decimal')
        memory=chart(name+' memory and limit','enmaas_container_memory_bytes'+selector,'components',
            name+' container memory in MiB/GiB, including cache as reported by Podman. The second line is its configured ceiling. Short spikes or an OOM between samples may not be visible; inspect failure evidence and telemetry gaps.',unit='bytes')
        memory['spec']['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{
            'query':'enmaas_container_memory_limit_bytes'+selector,'seriesNameFormat':'{{host}} / {{replica}} — limit'}}}})
        for panel in (cpu,memory):
            panel['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='{{host}} / {{replica}} — usage'
        panels.extend([(key+'cpu',cpu,12,8),(key+'memory',memory,12,8)])
    panels.append(('databasesection',section('Database — selected PostgreSQL source',
        'Database selects the monitored PostgreSQL engine independently of Host. These engine measurements apply to local PostgreSQL or RDS when a collector supplies them. '
        'No source means collection is unavailable, not zero activity. RDS CloudWatch CPU, IOPS, storage latency and credits require a separate integration; they are not Linux host metrics.'),24,3))
    for key,title,query,unit,desc in (
      ('dbconnections','Database connections','enmaas_postgres_connections{host=~"$database"}','decimal','Connections to the metering database. The server connection limit is shared by all databases, so this is not a whole-server utilization percentage.'),
      ('dbsize','Database size','enmaas_postgres_size_bytes{host=~"$database"}','bytes','Metering database size; excludes unrelated databases and WAL. Not the RDS allocated storage size.'),
      ('dbtransactions','Committed transactions per second','rate(enmaas_postgres_transactions_committed_total{host=~"$database"}[2m])','decimal','PostgreSQL engine transaction rate, separate from inference requests.'),
      ('dbwaiters','Database lock waiters','enmaas_postgres_lock_waiters{host=~"$database"}','decimal','Sessions waiting on a database lock; distinct from PriceTag connection-pool waiting.'),
      ('dbdeadlocks','Database deadlocks — last 2 minutes','increase(enmaas_postgres_deadlocks_total{host=~"$database"}[2m])','decimal','Deadlocks detected by PostgreSQL; zero is valid only with a present source.'),
      ('dbtemp','Database temporary writes','rate(enmaas_postgres_temp_bytes_total{host=~"$database"}[2m])','bytes/sec','Temporary file writes reported by PostgreSQL. This is not physical disk IOPS or storage latency.')):
        panels.append((key,chart(title,query,'accounting',desc,unit=unit),12,7))
    panels.append(('accountingsection',section('Metering and alerts — fleet-wide',
      'Usage recording correctness is independent of successful inference. These service-wide metrics and alerts are not filtered by Host. '
      'Benchmark workload is excluded from these operational views.'),24,2))
    for key in ('accountingfailures','dbsessions'):
        panels.append((key,copy.deepcopy(source[key]),6,3))
    panels.append(('firing',chart('Firing operational alerts','ALERTS{alertstate="firing",category!="generator",alertname=~"Enmaas.*"}','alerts','Fleet-wide operational alerts. Host/service/instance identify the affected resource when supplied by the rule. Empty means no firing alerts.','Table',True),24,9))
    document=resource_selectors(dashboard('monitoring','enMAAS — main monitoring',panels))
    document['spec']['variables'].append({'kind':'ListVariable','spec':{
        'name':'response_class','display':{'name':'Response classes'},
        'allowMultiple':True,'allowAllValue':True,'customAllValue':'.*',
        'defaultValue':['2xx','1xx','unknown'],
        'plugin':{'kind':'StaticListVariable','spec':{'values':[
            {'value':'2xx','label':'2xx — successes (including 200)'},
            {'value':'3xx','label':'3xx — redirects'},
            {'value':'4xx','label':'4xx — client / policy rejections'},
            {'value':'5xx','label':'5xx — server errors'},
            {'value':'1xx','label':'Other — 1xx informational'},
            {'value':'unknown','label':'Other — unclassified'},
        ]}}}})
    document['spec']['variables'].append({'kind':'ListVariable','spec':{'name':'database','display':{'name':'Database'},
        'allowMultiple':True,'allowAllValue':True,'defaultValue':'$__all',
        'plugin':{'kind':'PrometheusLabelValuesVariable','spec':{'labelName':'host','matchers':['enmaas_postgres_size_bytes']}}}})
    return document


def navigation():
    return {'kind':'Panel','spec':{'display':{'name':'Monitoring navigation'},'plugin':{'kind':'Markdown','spec':{'text':
      ' · '.join('['+label+']('+BASE+'enmaas-'+target+')' for label,target in (
        ('Main monitoring','monitoring'),('Gateway traffic','gateway'),('Host','host'),('Components','components'),
        ('Database & usage metering','accounting'),('Alerts','alerts'),('Inference traces','traces'),('Service events','logs')))}}}}


def dashboard(name,title,panels):
    if name!='monitoring':panels=[('navigation',navigation(),24,3),*panels]
    items=[]; y=0; x=0; row_height=0
    for key,panel,width,height in panels:
        if x+width>24:y+=row_height; x=0; row_height=0
        items.append({'x':x,'y':y,'width':width,'height':height,'content':{'$ref':'#/spec/panels/'+key}})
        x+=width;row_height=max(row_height,height)
        if x>=24:y+=row_height; x=0; row_height=0
    return {'kind':'Dashboard','metadata':{'name':'enmaas-'+name,'project':'praxis'},'spec':{
      'display':{'name':title},'duration':'2h','refreshInterval':'15s',
      'links':[link('Main monitoring','monitoring'),link('Gateway traffic','gateway'),link('Host resources','host'),link('Database & usage metering','accounting'),link('Components','components'),link('Traces','traces'),link('Service events','logs')],
      'panels':{key:panel for key,panel,_,_ in panels},'layouts':[{'kind':'Grid','spec':{'items':items}}]}}


def resource_selectors(document,services=False):
    """Filter resource series without combining hosts or erasing their labels."""
    def variable(name,title,matcher):
        return {'kind':'ListVariable','spec':{'name':name,'display':{'name':title},
          'allowMultiple':True,'allowAllValue':True,'defaultValue':'$__all',
          'plugin':{'kind':'PrometheusLabelValuesVariable','spec':{'labelName':name,'matchers':[matcher]}}}}
    variables=[variable('host','Host','enmaas_host_sample_unixtime_seconds{host!=""}')]
    if services:
        variables.append(variable('component','Service','enmaas_container_memory_bytes{host=~"$host"}'))
        variables.append(variable('replica','Instance','enmaas_container_memory_bytes{host=~"$host",component=~"$component"}'))
        # Keep historical samples recorded before instance labels were introduced.
        variables[-1]['spec']['customAllValue']='.*'
    document['spec']['variables']=variables
    if services:
        document['spec']['panels']['navigation']['spec']['plugin']['spec']['text']+='\n\nInstance **All** includes historical samples without instance labels. Selecting an instance shows only samples carrying that label.'
    for panel in document['spec']['panels'].values():
        spec=panel['spec']
        for query in spec.get('queries',[]):
            plugin=query['spec']['plugin']['spec']
            def selected(match):
                labels=['host=~"$host"']
                if services:labels.extend(['component=~"$component"','replica=~"$replica"'])
                if match.group(2):labels.append(match.group(2))
                return match.group(1)+'{'+','.join(labels)+'}'
            plugin['query']=re.sub(r'\b(enmaas_(?:host|container)_[a-z_]+)(?:\{([^}]*)\})?',selected,plugin['query'])
        if spec['plugin']['kind']=='TimeSeriesChart' and any('enmaas_host_' in q['spec']['plugin']['spec']['query'] or 'enmaas_container_' in q['spec']['plugin']['spec']['query'] for q in spec.get('queries',[])):
            spec['plugin']['spec']['legend']={'mode':'table','position':'right','size':'medium','values':[] if spec['plugin']['spec'].get('querySettings') else ['max']}
    return document


def dashboards():
    accounting=dashboard('accounting','enMAAS — Database & usage metering',[
      ('pool',chart('Database pool connections','{__name__=~"metering_db_pool_(open|in_use|idle)_conns"}','accounting','PriceTag pool only; not every PostgreSQL connection.'),12,10),
      ('wait',chart('Database pool wait seconds per second','sum(rate(metering_db_pool_wait_seconds_total[2m]))','accounting','Aggregate wait time can exceed one second per second with multiple waiters.'),12,10),
      ('ingest',chart('Usage recording rate','sum(rate(metering_events_ingested_total[2m]))','accounting','Usage events accepted per second.',unit='events/sec'),12,10),
      ('errors',chart('Usage recording errors per second','sum(rate(metering_usage_event_insert_errors_total[2m]))','accounting','Failed usage inserts per second. Any sustained failure threatens usage-record completeness.',unit='events/sec'),12,10),
      ('latency',chart('Usage recording p95 duration','histogram_quantile(0.95,sum by(le)(rate(metering_usage_event_insert_duration_seconds_bucket[2m])))','accounting','95th-percentile insert duration. Requires activity; an idle histogram is undefined.',unit='seconds'),12,10),
      ('quota',chart('Quota checks and denials','{__name__=~"metering_quota_(checks|denials)_total"}','accounting','Cumulative counters; budget denials are policy decisions.'),12,10)])
    database_panels=[
      ('dbconnections','PostgreSQL connections','enmaas_postgres_connections','Connections to the metering database. The server connection limit is shared with other databases.','decimal'),
      ('dbactive','PostgreSQL active sessions','enmaas_postgres_active_connections','Active sessions in the metering database, excluding the sampling query.','decimal'),
      ('dblocks','PostgreSQL lock waiters','enmaas_postgres_lock_waiters','Sessions currently waiting for a lock in the metering database.','decimal'),
      ('dbcommits','PostgreSQL commits per second','rate(enmaas_postgres_transactions_committed_total[2m])','Committed transactions per second. Includes management and dashboard work, not just inference usage recording.','decimal'),
      ('dbrollback','PostgreSQL rollbacks per second','rate(enmaas_postgres_transactions_rolled_back_total[2m])','Rolled-back transactions per second. Inspect database logs to determine the cause.','decimal'),
      ('dbdeadlocks','PostgreSQL deadlocks','increase(enmaas_postgres_deadlocks_total[2m])','Detected deadlocks during the last two minutes; not ordinary lock waits.','decimal'),
      ('dbtemp','PostgreSQL temporary writes','rate(enmaas_postgres_temp_bytes_total[2m])','Temporary-file bytes written per second by database queries.','bytes/sec'),
      ('dbsize','Metering database size','enmaas_postgres_size_bytes','Current metering database size including indexes. Does not include the entire PostgreSQL cluster or WAL.','bytes')]
    for key,title,query,description,unit in database_panels:
        accounting['spec']['panels'][key]=chart(title,query,'accounting',description,unit=unit)
    connections=accounting['spec']['panels']['dbconnections']['spec']
    connections['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='{{host}} · metering connections'
    connections['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{
      'query':'enmaas_postgres_connection_limit','instant':False,'seriesNameFormat':'{{host}} · server connection limit'}}}})
    accounting['spec']['panels']['reportingcoverage']=section('Usage verification',
      'The pinned gateway does not consistently expose its usage-report failure counter. An absent counter is not proof of successful delivery. '
      'Compare inference completions with stored usage and reconcile stored requests, tokens and USD before making usage-delivery claims.')
    # Rebuild the grid so database panels remain ordinary navigable panels.
    accounting=dashboard('accounting','enMAAS — Database & usage metering',[
      (key,panel,24 if key=='reportingcoverage' else 12,3 if key=='reportingcoverage' else 9) for key,panel in accounting['spec']['panels'].items() if key!='navigation'])
    main=production_overview(overview_sources())
    alerts=dashboard('alerts','enMAAS — alert detail',[
        ('firing',chart('All current firing alerts','ALERTS{alertstate="firing",alertname=~"Enmaas.*"}','monitoring','Firing rules at the selected end time, grouped by category and severity.','Table',True),24,12),
        ('pending',chart('Pending alerts','ALERTS{alertstate="pending",alertname=~"Enmaas.*"}','monitoring','Conditions not yet sustained for their configured duration.','Table',True),24,8),
        ('history',chart('Alert history','ALERTS{alertname=~"Enmaas.*"}','monitoring','Historical pending/firing state in the selected window.'),24,10),
        ('guide',{'kind':'Panel','spec':{'display':{'name':'Alert response guide'},'plugin':{'kind':'Markdown','spec':{'text':'\n\n'.join('**'+name+' · '+category+' · '+severity+'** — '+summary+'. '+action+'. [Details]('+BASE+'enmaas-'+detail+')' for name,category,severity,expr,duration,summary,action,detail in RULES)}}}},24,24)])
    for panel in alerts['spec']['panels'].values():
        for query in panel['spec'].get('queries',[]):
            plugin=query['spec']['plugin']['spec']
            plugin['query']=plugin['query'].replace('ALERTS{','ALERTS{category!="generator",')
    alerts['spec']['panels']['guide']['spec']['plugin']['spec']['text']='\n\n'.join(
        '**'+name+' · '+category+' · '+severity+'** — '+summary+'. '+action+'. [Details]('+BASE+'enmaas-'+detail+')'
        for name,category,severity,expr,duration,summary,action,detail in RULES if category!='generator')
    components=dashboard('components','enMAAS — component resources',[
      ('cpu',chart('Service CPU — cores',COMPONENT_CPU,'components','CPU seconds per second, per host and service. No CPU limit is inferred.',unit='decimal'),24,10),
      ('memory',chart('Component memory','enmaas_container_memory_bytes','components','Podman-reported memory in MiB/GiB; may include reclaimable cache. Not host available memory.',unit='bytes'),24,10),
      ('memorylimit',chart('Container memory limits','enmaas_container_memory_limit_bytes','components','Configured memory ceiling for each container, scaled to MiB/GiB.',unit='bytes'),24,10),
      ('limits',chart('Memory limit utilization','100 * enmaas_container_memory_bytes / (enmaas_container_memory_limit_bytes > 0)','components','Usage divided by each container’s own memory limit. Do not sum these percentages.',unit='percent'),24,10),
      ('pids',chart('Component processes and threads','enmaas_container_pids','components','Current processes and threads in each container.'),24,10)])
    components['spec']['panels']['limits']['spec']['plugin']['spec']['yAxis'].update(min=0,max=100)
    host=dashboard('host','enMAAS — host resources',[
      ('cpu',chart('Host CPU busy',CPU,'host','Busy CPU share per host. Each host has its own 100% capacity; idle and I/O wait are excluded.',unit='percent'),24,8),
      ('memory',chart('Host memory','enmaas_host_memory_bytes{state=~"total|available|unavailable|cached"}','host','Available memory includes reclaimable memory. Cached overlaps other values; do not add it to unavailable.',unit='bytes'),24,8),
      ('disk',chart('Data filesystem','enmaas_host_disk_bytes','host','Capacity and available bytes on the sampled data filesystem, separately for each host.',unit='bytes'),24,8),
      ('load',chart('Host load averages','enmaas_host_load','host','Runnable and uninterruptible tasks over each load interval. Compare with that host’s CPU count; this is not a percentage.'),24,8)])
    host['spec']['panels']['cpu']['spec']['plugin']['spec']['yAxis'].update(min=0,max=100)
    for key in ('memory','disk','load'):
        host['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='{{host}} · {{state}}'
    return {'host-dashboard.json':resource_selectors(host),'components-dashboard.json':resource_selectors(components,True),'monitoring-dashboard.json':main,'accounting-dashboard.json':accounting,'alerts-dashboard.json':alerts}


def all_dashboards():
    """Combine maintained templates with generated dashboards without workspace writes."""
    documents={path.name:json.loads(path.read_text()) for path in (HERE/'perses').glob('*-dashboard.json')}
    documents.update(dashboards())
    for document in documents.values():
        for panel in document['spec']['panels'].values():
            plugin=panel['spec'].get('plugin',{})
            if plugin.get('kind')=='Markdown':
                plugin['spec']['text']=plugin['spec']['text'].replace('Accounting / DB','Database & usage metering')
    documents['gateway-dashboard.json']['spec']['panels']['1']['spec']['display']['description'] += ' Fast failures can lower this mean; compare successful throughput and response classes before judging latency.'
    documents['logs-dashboard.json']=log_filters(documents['logs-dashboard.json'])
    documents['gateway-dashboard.json']['spec']['panels']['0']['spec'].setdefault('links',[]).append(status_logs_link('5xx'))
    return documents


def write(root):
    root.mkdir(parents=True,exist_ok=True)
    for name,document in all_dashboards().items():
        (root/name).write_text(json.dumps(document,indent=2)+'\n')
    (root/'monitoring-rules.yaml').write_text(yaml.safe_dump(rules(),sort_keys=False))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Export dashboard and alert definitions for inspection')
    parser.add_argument('--output',type=Path,required=True,help='Output directory, for example .state/monitoring-preview')
    write(parser.parse_args().output)
