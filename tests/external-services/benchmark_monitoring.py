"""Opt-in benchmark monitoring; operational service definitions stay independent."""
from pathlib import Path
import importlib.util
import json
import yaml

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('operational_monitoring', HERE / 'monitoring.py')
operational = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(operational)
BASE = operational.BASE
MEAN = operational.MEAN
section = operational.section
link = operational.link
current = operational.current
dashboard = operational.dashboard
resource_selectors = operational.resource_selectors


def chart(*args, **kwargs):
    panel = operational.chart(*args, **kwargs)
    query = panel['spec']['queries'][0]['spec']['plugin']['spec']
    if query['query'].startswith('enmaas_benchmark_phase'):
        query['seriesNameFormat'] = '{{phase}}'
    return panel

RULES = [('BenchmarkFailuresHigh',
  'generator',
  'critical',
  '((enmaas_stress_completed - enmaas_stress_successful) / clamp_min(enmaas_stress_completed, 1) > 0.05) and '
  'on() (enmaas_stress_completed >= 100) and on() (time() - enmaas_stress_sample_unixtime_seconds < 30)',
  '1m',
  'More than 5% of completed benchmark requests failed',
  'Inspect client errors and HTTP outcomes; in-flight requests are excluded and failures are not '
  'automatically gateway faults',
  'benchmark'),
 ('GeneratorConcurrencyLimited',
  'generator',
  'warning',
  '(enmaas_stress_skipped_concurrency / enmaas_stress_offered_slots > 0.01) and on() (time() - '
  'enmaas_stress_sample_unixtime_seconds < 30)',
  '15s',
  'Generator concurrency cap skips offered requests',
  'Do not claim gateway capacity; compare active requests with generator cap',
  'benchmark'),
 ('GeneratorSchedulerLimited',
  'generator',
  'warning',
  '(enmaas_stress_skipped_scheduler / enmaas_stress_offered_slots > 0.01) and on() (time() - '
  'enmaas_stress_sample_unixtime_seconds < 30)',
  '15s',
  'Generator scheduling misses offered requests',
  'Inspect generator CPU and event-loop delay before increasing offered traffic',
  'benchmark')]

def benchmark_overview(source):
    """Keep load-test results separate from operational health."""
    import copy
    panels=[('intro',section('Benchmark',
        'Select the run’s time window. Cards show the selected point in its phase; charts show the full progression. '
        'Offered traffic is not achieved throughput. The run report verifies stored usage and cleanup. '
        '[Host and gateway resources]('+BASE+'enmaas-monitoring)'),24,3)]
    for key in ('generatordispatched','streamsuccess','currentgenerator','currentgeneratorskips'):
        panel=copy.deepcopy(source[key]);panel['spec']['links']=[link('Benchmark details','benchmark')]
        panels.append((key,panel,6,3))
    rates=chart('Offered, dispatched and successful traffic','enmaas_benchmark_offered_rps','benchmark',
        'Offered target versus phase-average dispatch and successful completion rates. Long streams complete after admission; drain time is included in the averages.',unit='requests/sec')
    rates['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='Offered target'
    for title,query in (('Dispatched','enmaas_stress_dispatched / clamp_min(enmaas_stress_elapsed_seconds, 1)'),('HTTP 200 completed','enmaas_stress_successful / clamp_min(enmaas_stress_elapsed_seconds, 1)')):
        rates['spec']['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':query,'seriesNameFormat':title}}}})
    panels.append(('rates',rates,24,8))
    panels.append(('population',section('Human and bot workload equivalents',
        'Two alternative demand models, not observed identities or supported seat counts. '
        '**People:** one model request per minute per active person. **Bots:** ten model requests per minute per active bot. '
        'Each chart compares the offered target with achieved dispatch. Do not add the two estimates: each assumes all traffic comes from that category. '
        'A real mix depends on measured request frequency and the human/bot traffic split.'),24,3))
    for key,title,factor,assumption in (
        ('people','Expected active people — 1 request/minute',60,'one request per minute per active person'),
        ('bots','Expected active bots — 10 requests/minute',6,'ten requests per minute per active bot')):
        panel=chart(title,f'{factor} * enmaas_benchmark_offered_rps','benchmark',
            f'Alternative all-{key} demand estimate assuming {assumption}; not observed users or a capacity guarantee. '
            'Offered target and achieved dispatch are multiplied by the assumed interval. Dispatch is averaged from phase start, including stream drain. '
            'Do not add this to the other population estimate.',unit='decimal')
        panel['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='Offered workload equivalent'
        panel['spec']['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{
            'query':f'{factor} * enmaas_stress_dispatched / clamp_min(enmaas_stress_elapsed_seconds, 1)',
            'seriesNameFormat':'Dispatched workload equivalent'}}}})
        panels.append((key,panel,12,8))
    for key,title,query,unit,desc in (
        ('concurrency','Active requests and generator capacity','(enmaas_stress_dispatched - enmaas_stress_completed)','decimal','Dispatched requests without a terminal outcome, including response validation. Derived from outcome counters so completed tasks waiting for cleanup callbacks are excluded. The configured ceiling is a generator constraint, not gateway capacity.'),
        ('skips','Admissions skipped','enmaas_stress_skipped_concurrency + enmaas_stress_skipped_scheduler','decimal','Per-phase admissions not dispatched because of concurrency or scheduler limits.'),
        ('failures','Completed requests without HTTP 200','enmaas_stress_completed - enmaas_stress_successful','decimal','Per-phase non-success outcomes. Consult the report for status and transport-error breakdown.'),
        ('phase','Load stages','enmaas_benchmark_phase','decimal','One active phase at a time; zero is inactive. Stage changes reset generator phase counters.')):
        panel=chart(title,query,'benchmark',desc,unit=unit)
        if key=='concurrency':
            panel['spec']['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{'query':'enmaas_stress_concurrency_limit','seriesNameFormat':'Configured ceiling'}}}})
        panels.append((key,panel,12,7))
    panels.append(('firing',chart('Firing benchmark alerts','ALERTS{alertstate="firing",category="generator"}','benchmark','Benchmark admission and client-outcome alerts. Operational service alerts are in Main monitoring.','Table',True),24,7))
    for key,title,query,unit,description in (
        ('loadcpu','Load versus service CPU — cores','rate(enmaas_container_cpu_seconds_total{component=~"gateway-sso|maas|metering-sso|db|provider"}[2m])','decimal','Left axis: CPU cores per selected host/service/instance. Right axis: achieved generator dispatch rate, averaged from phase start. The mock provider and generator can constrain throughput independently of the gateway.'),
        ('loadmemory','Load versus service memory','enmaas_container_memory_bytes{component=~"gateway-sso|maas|metering-sso|db|provider"}','bytes','Left axis: container memory per selected host/service/instance, including cache as reported by Podman. Right axis: achieved dispatch rate. Retained memory alone does not establish a leak.'),
        ('loadpool','Load versus database pool waiting','sum(rate(metering_db_pool_wait_seconds_total[2m]))','decimal','Left axis: aggregate connection-pool waiting seconds per second. Concurrent waiters can make this exceed one. Right axis: achieved dispatch rate. This is application pool waiting, not PostgreSQL lock waiting.'),
        ('loadduration','Load versus inference duration',MEAN,'seconds','Left axis: mean duration of completed inference streams, including provider response time. Right axis: achieved dispatch rate. Compare with the configured 10–120 second response schedule; this is not proxy overhead.')
    ):
        panel=chart(title,query,'components' if key in ('loadcpu','loadmemory') else 'accounting' if key=='loadpool' else 'gateway',description,unit=unit)
        panel['spec']['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{
            'query':'enmaas_stress_dispatched / clamp_min(enmaas_stress_elapsed_seconds, 1)','seriesNameFormat':'Dispatched requests/s'}}}})
        panel['spec']['plugin']['spec']['querySettings']=[{'queryIndex':1,'format':{'unit':'requests/sec','decimalPlaces':1},'lineStyle':'dashed'}]
        panel['spec']['plugin']['spec']['legend']['values']=[]
        panels.append((key,panel,24,8))
    panels.append(('mockintro',section('Mock provider performance',
        'The mock backend consumes CPU and memory independently of Praxis. Compare its resource use with the gateway before attributing failures to gateway capacity. '
        'The configured response schedule is not measured backend latency; completed-stream duration includes the entire request path. '
        'Missing resource samples do not prove downtime or OOM: confirm those using container state and kernel evidence in the run report.'),24,3))
    panels.append(('mockcpu',chart('Mock provider CPU — cores',
        'rate(enmaas_container_cpu_seconds_total{component="provider"}[2m])','benchmark',
        'CPU cores consumed by the mock provider per host and instance. One core is one fully occupied logical CPU; this is not a percentage or gateway CPU.',unit='decimal'),12,8))
    memory=chart('Mock provider memory and limit',
        'enmaas_container_memory_bytes{component="provider"}','benchmark',
        'Container memory, including cache reported by Podman, against its configured ceiling. A high value indicates mock-backend pressure, not gateway pressure. Gaps are missing samples, not zero usage.',unit='bytes')
    memory['spec']['queries'][0]['spec']['plugin']['spec']['seriesNameFormat']='{{host}} / {{replica}} — memory'
    memory['spec']['queries'].append({'kind':'TimeSeriesQuery','spec':{'plugin':{'kind':'PrometheusTimeSeriesQuery','spec':{
        'query':'enmaas_container_memory_limit_bytes{component="provider"}','seriesNameFormat':'{{host}} / {{replica}} — limit'}}}})
    memory['spec']['plugin']['spec']['querySettings']=[{'queryIndex':1,'lineStyle':'dashed'}]
    panels.append(('mockmemory',memory,12,8))
    document=resource_selectors(dashboard('benchmark','enMAAS — benchmark results',panels))
    for key in ('mockcpu','mockmemory'):
        document['spec']['panels'][key]['spec']['plugin']['spec']['legend']={'mode':'list','position':'bottom','values':[]}
    return document

def overview_sources():
    fresh = 'time() - max(enmaas_stress_sample_unixtime_seconds) < 30'
    return {
        'generatordispatched': current('Generator dispatch rate',
            'enmaas_stress_dispatched / clamp_min(enmaas_stress_elapsed_seconds, 1)',
            'benchmark', 'Phase-average dispatched requests per second, including in-flight requests.', 'requests/sec', fresh),
        'streamsuccess': current('Successful requests',
            'enmaas_stress_successful / clamp_min(enmaas_stress_elapsed_seconds, 1)',
            'benchmark', 'Phase-average HTTP 200 completions, distinct from dispatch and all-status completions.', 'requests/sec', fresh),
        'currentgenerator': current('Generator concurrency',
            '100 * (enmaas_stress_dispatched - enmaas_stress_completed) / enmaas_stress_concurrency_limit',
            'benchmark', 'Dispatched minus completed outcomes as a percentage of the generator cap. This is not gateway capacity.', 'percent', fresh, (80, 95)),
        'currentgeneratorskips': current('Generator skipped admissions',
            '100 * (enmaas_stress_skipped_concurrency + enmaas_stress_skipped_scheduler) / enmaas_stress_offered_slots',
            'benchmark', 'Offered admissions skipped by concurrency or scheduler limits. These requests never reached the gateway.', 'percent', fresh, (1, 10)),
    }


def rules():
    groups = operational.rules()['groups']
    groups.append({'name': 'enmaas-benchmark', 'interval': '15s', 'rules': [
        {'alert': 'Enmaas' + name, 'expr': expr, 'for': duration,
         'labels': {'severity': severity, 'category': category, 'summary': summary,
                    'action': action, 'detail': detail, 'component': 'generator'},
         'annotations': {'summary': summary, 'description': action,
                         'dashboard': BASE + 'enmaas-' + detail}}
        for name, category, severity, expr, duration, summary, action, detail in RULES
    ]})
    return {'groups': groups}


def all_dashboards():
    return {**operational.all_dashboards(),
            'benchmark-dashboard.json': benchmark_overview(overview_sources())}


def write(root):
    root.mkdir(parents=True, exist_ok=True)
    for name, document in all_dashboards().items():
        (root / name).write_text(json.dumps(document, indent=2) + '\n')
    (root / 'monitoring-rules.yaml').write_text(yaml.safe_dump(rules(), sort_keys=False))
