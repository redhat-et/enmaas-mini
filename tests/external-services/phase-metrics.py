"""Bounded scenario annotations, not synthetic application performance metrics."""
import time

NAMES=('budget_mix','stress_200','stress_250','stress_500','stress_750','baseline','team','parallel','bursts','sustained_high','rate_limited','recovery','stress_10','stress_50','stress_150','stress_100','stress_1000','stress_10000','stress_recovery')


def payload(phase,now=None):
    if phase['name'] not in (*NAMES,'idle'):raise ValueError('Unknown dashboard phase')
    now=time.time_ns() if now is None else now
    def point(value,attributes=()):
        return {'timeUnixNano':str(now),'asDouble':value,'attributes':list(attributes)}
    points=[point(int(name==phase['name']),[{'key':'phase','value':{'stringValue':name}}]) for name in NAMES]
    metrics=[{'name':'enmaas_benchmark_phase','gauge':{'dataPoints':points}}]
    for name,key in [('users','users'),('lanes_per_user','streams_per_user'),('offered_rps','offered_rps'),('rate_limit_rps','rate_limit_rps'),('cycle','cycle')]:
        value=phase.get(key,0 if phase['name']=='idle' else 1)
        ceiling={'users':2500,'offered_rps':10000,'rate_limit_rps':20000}.get(key,2000)
        if type(value) not in (int,float) or not (0<=value<=ceiling if phase['name']=='idle' else 0<value<=ceiling):raise ValueError('Invalid scenario annotation')
        metrics.append({'name':'enmaas_benchmark_'+name,'gauge':{'dataPoints':[point(value)]}})
    metrics.append({'name':'enmaas_benchmark_sample_unixtime_seconds','gauge':{'dataPoints':[point(now/1e9)]}})
    return {'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-benchmark'}}]},'scopeMetrics':[{'metrics':metrics}]}]}
