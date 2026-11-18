#!/usr/bin/env python3
"""Prove the collector preserves source sample time instead of refreshing cached data."""
import secrets
import time
import urllib.parse
import signals


def main():
    name='enmaas-freshness-'+secrets.token_hex(4)
    sent=time.time_ns()-45_000_000_000
    signals.send('metrics',{'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':name}}]},
      'scopeMetrics':[{'metrics':[{'name':'enmaas_freshness_probe','gauge':{'dataPoints':[{'asInt':'7','timeUnixNano':str(sent)}]}}]}]}]})
    query='timestamp(enmaas_freshness_probe{exported_job="'+name+'"})'
    for _ in range(20):
        reply=signals.query('http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({'query':query})) or {}
        rows=reply.get('data',{}).get('result',[])
        if rows:
            if len(rows)!=1 or abs(float(rows[0]['value'][1])-sent/1e9)>0.01:
                raise RuntimeError('Source sample timestamp was not preserved')
            print('PASS: original metric timestamp survives collection and Prometheus scraping');return
        time.sleep(2)
    raise RuntimeError('Timestamp test metric did not arrive')

if __name__=='__main__':main()
