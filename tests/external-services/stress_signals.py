"""Generator telemetry, distinct from measurements made by the gateway."""
import math
import time

FIELDS=('offered_slots','dispatched','skipped_concurrency','skipped_scheduler',
        'active_requests','completed','elapsed_seconds')


def payload(snapshot, concurrency, now=None):
    if type(concurrency) is not int or not 1<=concurrency<=40000:raise ValueError('Invalid concurrency limit')
    timestamp=time.time_ns() if now is None else now
    metrics=[]
    for name in FIELDS:
        value=snapshot[name]
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100000000:
            raise ValueError('Invalid generator metric')
        metrics.append({'name':'enmaas_stress_'+name,'gauge':{'dataPoints':[{'asDouble':value,'timeUnixNano':str(timestamp)}]}})
    for name in ('request_bytes','response_bytes','stream_events','successful'):
        value = snapshot.get('statuses',{}).get('200',0) if name=='successful' else snapshot.get(name,0)
        if type(value) is not int or not 0<=value<=10**15:raise ValueError('Invalid traffic metric')
        metrics.append({'name':'enmaas_stress_'+name,'gauge':{'dataPoints':[{'asDouble':value,'timeUnixNano':str(timestamp)}]}})
    for name,value in (('concurrency_limit',concurrency),('sample_unixtime_seconds',timestamp/1e9)):
        metrics.append({'name':'enmaas_stress_'+name,'gauge':{'dataPoints':[{'asDouble':value,'timeUnixNano':str(timestamp)}]}})
    return {'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-stress-generator'}}]},'scopeMetrics':[{'metrics':metrics}]}]}
