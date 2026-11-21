"""Bounded benchmark-generator signals, distinct from gateway telemetry."""
import time


def payloads(successes, now=None):
    if type(successes) is not int or not 0 <= successes <= 100000000:
        raise ValueError('Invalid benchmark success count')
    now = time.time_ns() if now is None else now
    resource = {'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-mock-benchmark'}}]}
    return {
        'metrics': {'resourceMetrics':[{'resource':resource,'scopeMetrics':[{'metrics':[{
            'name':'enmaas_benchmark_completed_requests','gauge':{'dataPoints':[{'asInt':str(successes),'timeUnixNano':str(now)}]}
        }]}]}]},
        'logs': {'resourceLogs':[{'resource':resource,'scopeLogs':[{'logRecords':[{
            'timeUnixNano':str(now),'severityNumber':9,'body':{'stringValue':'mock benchmark phase completed'}
        }]}]}]},
    }
