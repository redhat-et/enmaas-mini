#!/usr/bin/env python3
"""Verify the extracted telemetry stores and Perses in an unexposed disposable pod."""
import importlib.util
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('transport_privacy',HERE/'redaction.py')
privacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(privacy)
sys.path.insert(0,str(HERE.parents[1]/'scripts/pricetag'))
import prepare
spec=importlib.util.spec_from_file_location('query_samples',HERE/'monitoring-samples.py')
samples=importlib.util.module_from_spec(spec);spec.loader.exec_module(samples)

CLIENT='''import json,math,ssl,time,urllib.request,urllib.parse
from pathlib import Path
payloads=json.loads(Path('/lab/payloads.json').read_text())
context=ssl.create_default_context(cafile='/lab/ca.pem')
context.load_cert_chain('/lab/client.pem','/lab/client.key')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
def request(url,payload=None):
 data=json.dumps(payload).encode() if payload is not None else None
 with opener.open(urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'}),timeout=3) as response:
  return json.load(response)
def wait(check):
 for _ in range(45):
  try:
   if check():return
  except (OSError,ValueError):pass
  time.sleep(1)
 raise RuntimeError('telemetry acceptance deadline exceeded')
print('CHECK: Prometheus readiness',flush=True)
wait(lambda:request('http://127.0.0.1:9090/api/v1/status/buildinfo').get('status')=='success')
print('CHECK: Perses provisioning',flush=True)
wait(lambda:request('http://127.0.0.1:8080/api/v1/projects/praxis/dashboards/local-transport').get('metadata',{}).get('name')=='local-transport')
print('PASS: Perses provisioned the transport dashboard',flush=True)
def canonical(value):
 if isinstance(value,list):return [canonical(item) for item in value]
 if isinstance(value,dict):return {key:canonical(item) for key,item in value.items() if not (key=='targetBlank' and item is False or key=='description' and item=='')}
 return value
for expected in json.loads(Path('/lab/dashboard-index.json').read_text()):
 actual=request('http://127.0.0.1:8080/api/v1/projects/praxis/dashboards/'+expected['metadata']['name'])
 if canonical(actual['spec']['panels'])!=canonical(expected['spec']['panels']):raise AssertionError('provisioned panels differ from source')
print('PASS: all operational dashboard panels match source through Perses API',flush=True)
# Wait for the collector before sending: do not retry signal writes.
def collector_ready():
 try:request('https://127.0.0.1:24443/')
 except urllib.error.HTTPError as error:return error.code==404
 return False
print('CHECK: collector readiness',flush=True)
wait(collector_ready)
for signal,payload in payloads.items():
 if request('https://127.0.0.1:24443/v1/'+signal,payload).get('partialSuccess'):
  raise RuntimeError('partial signal admission')
urls={
 'metrics':'http://127.0.0.1:9090/api/v1/query?'+urllib.parse.urlencode({'query':'enmaas_lab_requests_total'}),
 'logs':'http://127.0.0.1:3100/loki/api/v1/query_range?'+urllib.parse.urlencode({'query':'{service_name="enmaas-gateway-contract"}'}),
 'traces':'http://127.0.0.1:3200/api/traces/1234567890abcdef1234567890abcdef'}
for signal,url in urls.items():
 print('CHECK: '+signal+' storage',flush=True)
 def found():
  data=request(url)
  if 'ENMAAS_SECRET_MUST_NOT_SURVIVE' in json.dumps(data):raise AssertionError('privacy sentinel survived in '+signal)
  return bool(data.get('batches') or data.get('resourceSpans')) if signal=='traces' else bool(data.get('data',{}).get('result'))
 wait(found)
 print('PASS: '+signal+' stored and queryable without seeded sensitive fields',flush=True)

print('CHECK: operational panel queries',flush=True)
fixtures=json.loads(Path('/lab/series.json').read_text())
started=time.time()
for frame in range(5):
 now=time.time();metrics=[]
 for name,labels,base,rate in fixtures:
  value=now if name=='enmaas_host_sample_unixtime_seconds' else base+rate*(now-started)
  metrics.append({'name':name,'gauge':{'dataPoints':[{'timeUnixNano':str(int(now*1e9)),
   'asDouble':value,'attributes':[{'key':key,'value':{'stringValue':value}} for key,value in labels.items()]}]}})
 body={'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-query-contract'}}]},'scopeMetrics':[{'metrics':metrics}]}]}
 request('https://127.0.0.1:24443/v1/metrics',body)
 time.sleep(5)
# Wait for the exporter batch and Prometheus scrape to observe the last frame.
wait(lambda:request('http://127.0.0.1:9090/api/v1/query?'+urllib.parse.urlencode({'query':'rate(enmaas_container_cpu_seconds_total[2m])'})).get('data',{}).get('result'))
checked=0
for dashboard in json.loads(Path('/lab/dashboard-index.json').read_text()):
 for key,panel in dashboard['spec']['panels'].items():
  for item in panel['spec'].get('queries',[]):
   plugin=item['spec']['plugin'];query=plugin['spec']['query']
   for variable,value in (('$response_class','(2xx|1xx|unknown)'),('$host','.+'),('$component','.+'),('$replica','.+'),('$database','.+'),('$log_service','.+'),('$log_status','.*')):
    query=query.replace(variable,value)
   if plugin['kind']=='PrometheusTimeSeriesQuery':
    data=request('http://127.0.0.1:9090/api/v1/query?'+urllib.parse.urlencode({'query':query}))
    if data.get('status')!='success':raise AssertionError('Prometheus rejected panel query')
    rows=data['data']['result']
    if 'ALERTS' not in query and not rows:raise AssertionError('missing series for '+dashboard['metadata']['name']+'/'+key)
    if any(not math.isfinite(float(row['value'][1])) for row in rows):raise AssertionError('non-finite panel value')
    if key=='currentcpu' and len(rows)!=2:raise AssertionError('host identity was aggregated away')
    if key=='currentcpu' and any(abs(float(row['value'][1])-30)>0.1 for row in rows):raise AssertionError('host CPU normalization mismatch')
    if key=='currentmemory' and any(abs(float(row['value'][1])-25)>0.1 for row in rows):raise AssertionError('host memory normalization mismatch')
   elif plugin['kind']=='LokiLogQuery':
    data=request('http://127.0.0.1:3100/loki/api/v1/query_range?'+urllib.parse.urlencode({'query':query}))
    if not data.get('data',{}).get('result'):raise AssertionError('log panel query returned no fixture event')
   elif plugin['kind']=='TempoTraceQuery':
    if query=='${traceId}':url='http://127.0.0.1:3200/api/traces/1234567890abcdef1234567890abcdef'
    else:url='http://127.0.0.1:3200/api/search?'+urllib.parse.urlencode({'q':query})
    data=request(url)
    if not (data.get('traces') or data.get('batches') or data.get('resourceSpans')):raise AssertionError('trace panel query returned no fixture trace')
   else:raise AssertionError('unqualified query plugin')
   checked+=1
print('PASS: '+str(checked)+' operational panel queries return expected finite signals (alert tables may be empty)',flush=True)
'''


def command(*args,input=None,timeout=60):
    result=subprocess.run(['podman',*args],input=input,capture_output=True,timeout=timeout)
    if result.returncode:
        # This client prints only fixed stage names and synthetic-data checks.
        if args[:1]==('run',) and name_is_client(args):
            for line in result.stdout.decode(errors='replace').splitlines():
                if line.startswith(('CHECK: ','PASS: ')):print(line,flush=True)
        raise RuntimeError('Telemetry fixture operation failed; other output suppressed')
    return result.stdout


def name_is_client(args):
    return '--name' in args and args[args.index('--name')+1].endswith('-client')


def cleanup(name, created, pod, network=False):
    operations=[('pod','rm','--force',name)] if pod else []
    operations.extend(('secret','rm',secret) for secret in reversed(created))
    if network:operations.append(('network','rm',name))
    failed=False
    for operation in operations:
        try:
            result=subprocess.run(['podman',*operation],capture_output=True,timeout=15)
            failed |= result.returncode != 0
        except (subprocess.TimeoutExpired,OSError):failed=True
    if failed:raise RuntimeError('Telemetry cleanup incomplete; inspect '+name)


def browser_namespace():
    # Rootful internal bridges do not provide a working published host port.
    # CI already runs as root: join only this disposable pod's network namespace.
    return (sys.platform == 'linux' and os.geteuid() == 0 and
            command('info', '--format', '{{.Host.Security.Rootless}}').strip() == b'false')


def browser_command(infra, namespace, index, evidence):
    prefix = []
    if namespace:
        pid = command('inspect', '--format', '{{.State.Pid}}', infra).decode().strip()
        if not pid.isdecimal() or int(pid) <= 1:
            raise RuntimeError('Invalid or stopped telemetry infra PID')
        prefix = ['nsenter', '--target', pid, '--net', '--']
        origin = 'http://127.0.0.1:8080'
    else:
        binding = command('port', infra, '8080/tcp').decode().strip()
        match = re.fullmatch(r'127[.]0[.]0[.]1:([0-9]+)', binding)
        if not match or not 0 < int(match[1]) <= 65535:
            raise RuntimeError('Browser requires one valid loopback port binding')
        origin = 'http://' + binding
    return [*prefix, 'node', str(HERE / 'monitoring-browser.cjs'), origin, str(index), str(evidence)]


def main(browser=False):
    name='enmaas-transport-'+secrets.token_hex(6)
    created=[];pod=False;network=False
    namespace = browser and browser_namespace()
    with tempfile.TemporaryDirectory(prefix=name+'-') as temporary:
        root=Path(temporary)
        privacy.obs.configure(root,'127.0.0.1','127.0.0.1','synthetic-client-secret-1234','10.0.0.12')
        privacy.certs.generate(root/'tls',{role:['127.0.0.1'] for role in privacy.certs.ROLES},1)
        files={name:(root/name).read_bytes() for name in ('perses.yaml','prometheus.yaml',
            'monitoring-rules.yaml','loki.yaml','tempo.yaml','collector.yaml',*privacy.obs.lab.PERSES_ASSETS)}
        for source,target in (('observability/server.pem','server.pem'),('observability/server.key','server.key'),
                              ('observability/ca.pem','ca.pem'),('observability/telemetry-ca.pem','telemetry-ca.pem'),
                              ('gateway/telemetry-client.pem','client.pem'),('gateway/telemetry-client.key','client.key')):
            files[target]=(root/'tls'/source).read_bytes()
        payloads=privacy.payloads()
        for signal,key in (('logs','resourceLogs'),('traces','resourceSpans')):
            payloads[signal][key][0]['resource']['attributes'][0]['value']['stringValue']='enmaas-gateway-contract'
        span=payloads['traces']['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        span['attributes'] += [{'key':'http.request.method','value':{'stringValue':'POST'}},
                               {'key':'url.path','value':{'stringValue':'/v1/responses'}}]
        files['series.json']=json.dumps(samples.series()).encode()
        metrics=payloads['metrics']['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        next(metric for metric in metrics if 'sum' in metric)['name']='enmaas_lab_requests'
        files['payloads.json']=json.dumps(payloads).encode()
        files['dashboard-index.json']=json.dumps(list(privacy.obs.lab.monitoring.all_dashboards().values())).encode()
        files['probe.py']=CLIENT.encode()
        try:
            command('network','create','--internal','--label','io.enmaas.test-lab=transport',name)
            network=True
            ports=['--publish','127.0.0.1::8080'] if browser and not namespace else []
            command('pod','create','--name',name,'--network',name,'--label','io.enmaas.test-lab=transport',*ports)
            pod=True
            for filename,content in files.items():
                secret=name+'-'+filename.replace('.','-')
                command('secret','create',secret,'-',input=content);created.append(secret)
            def mounts(names,prefix='/lab/'):
                result=[]
                for filename in names:
                    result+=['--secret',name+'-'+filename.replace('.','-')+',target='+prefix+filename+',mode=0444']
                return result
            options=['--pull=never','--pod',name,'--read-only','--cap-drop','all',
                     '--security-opt','no-new-privileges','--pids-limit','128']
            components=(
                ('perses',['perses.yaml'],['--config=/lab/perses.yaml'],'/perses','256m'),
                ('prometheus',['prometheus.yaml','monitoring-rules.yaml'],['--config.file=/lab/prometheus.yaml','--storage.tsdb.path=/prometheus'],'/prometheus','256m'),
                ('loki',['loki.yaml'],['-config.file=/lab/loki.yaml'],'/loki','384m'),
                ('tempo',['tempo.yaml'],['-config.file=/lab/tempo.yaml'],'/var/tempo','512m'),
                ('collector',['collector.yaml','server.pem','server.key','telemetry-ca.pem'],['--config=/lab/collector.yaml'],None,'256m'))
            for component,inputs,args,storage,memory in components:
                extra=mounts(inputs)
                service_options=list(options)
                # Perses expands its bundled plugin archives on first startup.
                # Match the RHEL Quadlet's writable container layer; mounting
                # an empty tmpfs here would hide those required archives.
                if component=='perses':service_options.remove('--read-only')
                if storage:extra+=['--tmpfs',storage+':rw,nosuid,nodev,mode=1777,size=128m']
                if component=='perses':extra+=mounts(privacy.obs.lab.PERSES_ASSETS,'/etc/perses/lab/')
                command('run','--detach','--name',name+'-'+component,*service_options,'--memory',memory,
                        *extra,privacy.obs.lab.IMAGES[component],*args)
            details=json.loads(command('pod','inspect',name))
            if isinstance(details,list):details=details[0]
            bindings=details.get('InfraConfig',{}).get('PortBindings') or {}
            if browser and not namespace:
                if set(bindings)!={'8080/tcp'} or any(entry['HostIp']!='127.0.0.1' for entry in bindings['8080/tcp']):
                    raise RuntimeError('browser probe must expose only a loopback Perses port')
            elif bindings:raise RuntimeError('unexpected published port')
            if not json.loads(command('network','inspect',name))[0]['internal']:
                raise RuntimeError('telemetry fixture network permits external traffic')
            result=command('run','--name',name+'-client',*options,'--memory','128m','--user','1001:1001',
                *mounts(('probe.py','payloads.json','dashboard-index.json','series.json','ca.pem','client.pem','client.key')),
                prepare.PYTHON,'python3','-B','/lab/probe.py',timeout=240)
            print(result.decode().strip(),flush=True)
            if browser:
                index=root/'dashboard-index.json';index.write_bytes(files['dashboard-index.json'])
                subprocess.run(browser_command(details['InfraContainerID'], namespace, index,
                    HERE.parents[1]/'.state/monitoring-browser'),check=True,timeout=240)
        finally:cleanup(name,created,pod,network)
    print('PASS: disposable telemetry pod and secrets removed; no persistent volumes')


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',action='store_true',help='also test rendering using a temporary loopback-only Perses port')
    main(parser.parse_args().browser)
