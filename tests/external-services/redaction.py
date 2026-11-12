#!/usr/bin/env python3
"""Black-box collector privacy regression with synthetic sentinel data only."""
import importlib.util
import argparse
import gzip
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import yaml

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('obs',HERE/'observability-host.py')
obs=importlib.util.module_from_spec(spec);spec.loader.exec_module(obs)
SENTINEL='ENMAAS_SECRET_MUST_NOT_SURVIVE'


def payloads():
    now=str(time.time_ns())
    attr=lambda key,value:{'key':key,'value':{'stringValue':value}}
    secret=[attr('authorization',SENTINEL),attr('prompt',SENTINEL)]
    resource={'attributes':[attr('service.name','enmaas-benchmark'),*secret]}
    scope={'name':SENTINEL,'version':SENTINEL,'attributes':secret}
    ids={'traceId':'1234567890abcdef1234567890abcdef','spanId':'1234567890abcdef'}
    result = {
      'logs':{'resourceLogs':[{'resource':resource,'scopeLogs':[{'scope':scope,'logRecords':[
        {'timeUnixNano':now,'body':{'stringValue':SENTINEL},'severityText':SENTINEL,
         'severityNumber':9,'attributes':secret,**ids}]}]}]},
      'traces':{'resourceSpans':[{'resource':resource,'scopeSpans':[{'scope':scope,'spans':[
        {**ids,'name':SENTINEL,'traceState':'vendor='+SENTINEL,'startTimeUnixNano':now,'endTimeUnixNano':str(int(now)+1000),
         'attributes':secret,'status':{'code':2,'message':SENTINEL},
         'events':[{'timeUnixNano':now,'name':SENTINEL,'attributes':secret}],
         'links':[{**ids,'traceState':'vendor='+SENTINEL,'attributes':secret}]}]}]}]},
      'metrics':{'resourceMetrics':[{'resource':resource,'scopeMetrics':[{'scope':scope,'metrics':[
        {'name':'enmaas.test','description':SENTINEL,'unit':SENTINEL,'gauge':{'dataPoints':[
          {'timeUnixNano':now,'asInt':'7','attributes':secret,'exemplars':[
            {'timeUnixNano':now,'asInt':'7','filteredAttributes':secret,**ids}]}]}}]}]}]}}


    metrics=result['metrics']['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
    for kind,point in (
        ('sum',{'asInt':'7'}),
        ('histogram',{'count':'1','sum':7,'bucketCounts':['1','0'],'explicitBounds':[10]}),
        ('exponentialHistogram',{'count':'1','sum':7,'scale':0,'zeroCount':'0','positive':{'offset':0,'bucketCounts':['1']}}),
        ('summary',{'count':'1','sum':7,'quantileValues':[{'quantile':0.5,'value':7}]})):
        point.update({'timeUnixNano':now,'attributes':secret})
        data={'dataPoints':[point]}
        if kind not in ('summary',):data['aggregationTemporality']=2
        if kind=='sum':data['isMonotonic']=True
        metrics.append({'name':'enmaas.test.'+kind,'description':SENTINEL,'unit':SENTINEL,kind:data})
    return result


def main(host_sample=None):
    data=payloads()
    expected_host={}
    if host_sample:
        with host_sample.open() as source: raw=source.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('Host sample exceeds bound')
        host=json.loads(raw)
        for resource in host['resourceMetrics']:
            for scope in resource['scopeMetrics']:
                for metric in scope['metrics']:
                    if not metric['name'].startswith('enmaas_host_'):raise ValueError('Expected aggregate host sample')
                    expected_host[metric['name']]=metric
        if len(expected_host)!=4:raise ValueError('Expected four host metrics')
        data['metrics']['resourceMetrics'].extend(host['resourceMetrics'])
    name='enmaas-redaction-'+uuid.uuid4().hex[:8]
    created=[];started=False
    def run(*args,**kwargs):
        return subprocess.run(['podman',*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs).stdout
    with tempfile.TemporaryDirectory(prefix='enmaas-redaction-') as temporary:
        root=Path(temporary);obs.configure(root,'127.0.0.1','127.0.0.1','synthetic-client-secret-1234','10.0.0.12')
        obs.lab.issue_certificates(root)
        config=yaml.safe_load((root/'collector.yaml').read_text())
        config['receivers']['otlp']['protocols']['http']['tls']['client_ca_file']='/lab/ca.pem'
        config['exporters']={f'file/{signal}':{'path':f'/output/{signal}.json','flush_interval':'100ms'} for signal in ('logs','metrics','traces')}
        for signal,pipeline in config['service']['pipelines'].items():pipeline['exporters']=[f'file/{signal}']
        (root/'collector.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
        try:
            mounts=[]
            for file in ('collector.yaml','ca.pem','server.pem','server.key'):
                secret=name+'-'+file.replace('.','-')
                with (root/file).open('rb') as source:run('secret','create','--label','io.enmaas.test-lab=redaction',secret,'-',stdin=source)
                created.append(secret);mounts+=['--secret',f'{secret},target=/lab/{file}']
            run('run','-d','--name',name,'--label','io.enmaas.test-lab=redaction',
                '--memory','256m','--pids-limit','128','--cap-drop','all','--security-opt','no-new-privileges',
                '-p','127.0.0.1::24443','--tmpfs','/output:rw,size=16m,mode=1777',
                *mounts,obs.lab.IMAGES['collector'],'--config=/lab/collector.yaml')
            started=True
            port=run('port',name,'24443/tcp').decode().strip().rsplit(':',1)[1]
            origin=f'https://127.0.0.1:{port}'
            ctx=ssl.create_default_context(cafile=str(root/'ca.pem'));ctx.load_cert_chain(root/'client.pem',root/'client.key')
            for _ in range(30):
                try:
                    urllib.request.urlopen(origin+'/',context=ctx,timeout=1)
                except urllib.error.HTTPError:break
                except OSError:time.sleep(.2)
            else:raise RuntimeError('collector did not start')
            # Valid JSON larger than the deployment's 4 MiB request bound. Test
            # both wire-size rejection and decompressed-size rejection.
            oversized=json.loads(json.dumps(data['logs']))
            oversized['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['body']={'stringValue':'x'*(4*1024*1024)}
            encoded=json.dumps(oversized).encode()
            for compressed in (False,True):
                headers={'Content-Type':'application/json'}
                body=encoded
                if compressed:
                    body=gzip.compress(body);headers['Content-Encoding']='gzip'
                request=urllib.request.Request(origin+'/v1/logs',data=body,headers=headers)
                try:
                    with urllib.request.urlopen(request,context=ctx,timeout=10):
                        raise RuntimeError('oversized telemetry request accepted')
                except urllib.error.HTTPError as error:
                    detail=error.read(8192).decode(errors='replace').lower()
                    if error.code not in (400,413) or 'too large' not in detail:
                        raise RuntimeError('oversized request did not receive explicit size rejection') from None
                print('PASS: oversized '+('gzip' if compressed else 'plain')+' telemetry body rejected')
            for signal,payload in data.items():
                request=urllib.request.Request(origin+'/v1/'+signal,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request,context=ctx,timeout=5) as response:
                    result=json.load(response)
                    if result.get('partialSuccess'):raise RuntimeError('collector rejected part of the sentinel test')
            for signal in data:
                destination=root/(signal+'.json')
                for _ in range(30):
                    try:
                        run('cp',name+':/output/'+signal+'.json',str(destination))
                        content=destination.read_text()
                        if content.strip():break
                    except subprocess.CalledProcessError:pass
                    time.sleep(.2)
                else:raise RuntimeError('no exported '+signal)
                if SENTINEL in content:raise RuntimeError('privacy sentinel survived in '+signal)
                if 'enmaas-benchmark' not in content:raise RuntimeError('required service identity lost in '+signal)
                documents=[json.loads(line) for line in content.splitlines() if line.strip()]
                if signal=='traces':
                    spans=[s for doc in documents for r in doc['resourceSpans'] for scope in r['scopeSpans'] for s in scope['spans']]
                    if len(spans)!=1 or spans[0]['traceId']!='1234567890abcdef1234567890abcdef':raise RuntimeError('trace correlation lost')
                if signal=='metrics':
                    metrics=[m for doc in documents for r in doc['resourceMetrics'] for scope in r['scopeMetrics'] for m in scope['metrics']]
                    if len(metrics)!=5+len(expected_host):raise RuntimeError('metric types were lost')
                    for metric in metrics:
                        if metric['name'] in expected_host:
                            kind='sum' if 'sum' in metric else 'gauge'
                            def values(item):
                                return {p['attributes'][0]['value']['stringValue']:float(p['asDouble']) for p in item[kind]['dataPoints']}
                            if values(metric)!=values(expected_host[metric['name']]):raise RuntimeError('Host values or state labels changed')
                if signal=='logs':
                    records=[r for doc in documents for resource in doc['resourceLogs'] for scope in resource['scopeLogs'] for r in scope['logRecords']]
                    if len(records)!=1 or records[0]['body'].get('stringValue')!='gateway event':raise RuntimeError('sanitized event lost')
                print('PASS: '+signal+' exported without seeded sensitive fields')
        finally:
            if started:run('rm','-f',name)
            for secret in created:run('secret','rm',secret)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-sample',type=Path,help='optional aggregate Linux sample for exact-value transport checks')
    main(parser.parse_args().host_sample)
