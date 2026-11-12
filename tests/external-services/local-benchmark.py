#!/usr/bin/env python3
"""Run the shared six-phase benchmark against the isolated local mock fixture."""
import argparse
from contextlib import contextmanager
import datetime
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid
import urllib.parse
import importlib.util

HERE=Path(__file__).resolve().parent

def load(file,name):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

fixture=load('gateway-sso.py','local_gateway_fixture')
bench=load('benchmark.py','local_benchmark_core')
lab=fixture.lab
signals=load('signals.py','benchmark_signals')

class SafetyStop(RuntimeError):
    """Fixed, nonsecret diagnostics that can be shown without private reports."""

class LocalController:
    save=bench.RhelController.save
    sample_due=bench.RhelController.sample_due

    def __init__(self,image,model=None):
        self.model=model or 'fixture';self.request_cost=Decimal('0.000008')
        self.image=image;self.original=None;self.lock=None;self.callers=[];self.created=[];self.secrets=[]
        self.telemetry=False;self.service_name='enmaas-gateway'
        self.name='lab-run-'+uuid.uuid4().hex
        self.output=lab.STATE/self.name
        self.last_sample=0
        self.summary={'mock_only':True,'generator':'local host','connection_policy':'new TLS connection per request',
                      'completed_mock_requests':{},'phases':[]}

    def api(self,route,body,method='PUT'):
        raw=fixture.run('run','--rm','--interactive','--pod',lab.POD,'--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',
            '--secret',lab.secret_name(self.name+'-management')+',target=/fixture/management,mode=0444',
            '--secret',lab.secret_name(self.name+'-client')+',target=/fixture/client.py,mode=0444',
            fixture.prepare.PYTHON,'python3','-B','/fixture/client.py',
            input=json.dumps({'route':route,'body':body,'method':method}).encode())
        return json.loads(raw)

    def prepare(self,count):
        self.lock=os.open(lab.STATE/'.benchmark.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        metadata=json.loads(fixture.run('pod','inspect',lab.POD))[0]
        if metadata.get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE:raise ValueError('Wrong pod owner')
        image=json.loads(fixture.run('image','inspect',self.image))[0]
        if image.get('Labels',{}).get('org.opencontainers.image.revision')!='5667c4acecd0981ae5114cbbf2ee3dcccec9c248':
            raise ValueError('Unexpected gateway revision')
        gateway=json.loads(fixture.run('inspect',lab.POD+'-gateway-sso'))[0]
        environment=dict(item.split('=',1) for item in gateway['Config'].get('Env',[]) if '=' in item)
        self.telemetry=environment.get('OTEL_EXPORTER_OTLP_ENDPOINT')=='http://127.0.0.1:4317'
        self.service_name=environment.get('OTEL_SERVICE_NAME','enmaas-gateway')
        self.original=(lab.STATE/'gateway-sso.json').read_bytes()
        config=json.loads(self.original)
        # The fixture itself must have no external model endpoints.
        endpoints=[endpoint for chain in config['filter_chains'] for filt in chain['filters']
                   for cluster in filt.get('clusters',[]) for endpoint in cluster.get('endpoints',[])]
        if any(not e.startswith(('127.0.0.1:','localhost:')) for e in endpoints):raise ValueError('Nonlocal backend')
        self.output.mkdir(mode=0o700)
        (self.output/'original.json').write_bytes(self.original)
        values=dict(line.split('=',1) for line in (lab.STATE/'gateway-sso.env').read_text().splitlines())
        if values.get('KEY_SERVICE_URL')!='http://127.0.0.1:8088':raise ValueError('MaaS fixture required')
        lab.write(self.output,'management',values['MANAGEMENT_API_SECRET'])
        for suffix,path in [('management',self.output/'management'),('client',HERE/'management-client.py')]:
            name=self.name+'-'+suffix;fixture.secret(name,path);self.secrets.append(name)
        if (lab.STATE/'mock-profiles.json').exists():
            from mock_profiles import configuration
            document=configuration()
            if json.loads((lab.STATE/'mock-profiles.json').read_text())!=document:
                raise ValueError('Mock profile manifest differs from the reviewed fixture')
            if self.model=='fixture':self.model=document['models'][0]['id']
            selected=next((m for m in document['models'] if m['id']==self.model and 'openai' in m['apis']),None)
            if selected is None:raise ValueError('Select an OpenAI-compatible mock alias')
            upstream=selected['model']
            if not re.fullmatch(r'[A-Za-z0-9_./-]+',upstream):raise ValueError('Invalid mock upstream ID')
            query="SELECT input_cost_per_mtok,output_cost_per_mtok FROM model_pricing WHERE model='"+upstream+"';"
            row=fixture.run('exec',lab.POD+'-db','psql','-U','keycloak','-d','metering_sso',
                            '-At','-v','ON_ERROR_STOP=1','-c',query).decode().strip().split('|')
            if len(row)!=2:raise ValueError('Mock model has no explicit imported price; select a priced model')
            self.request_cost=((Decimal(row[0])*2+Decimal(row[1])*3)/1000000).quantize(Decimal('0.00000001'),rounding=ROUND_HALF_UP)
            self.summary['model']=self.model
            self.summary['expected_usd_per_request']=str(self.request_cost)
        else:
            if self.model!='fixture':raise ValueError('Legacy mock only serves fixture')
            self.api('prices',{'model':'fixture','provider':'vllm','input_cost_per_mtok':1,'output_cost_per_mtok':2,
                               'cache_write_cost_per_mtok':0,'cache_read_cost_per_mtok':0})
        for index in range(count):
            subject=self.name+'-'+str(index)
            self.created.append(subject) # Cleanup also covers a committed write whose reply was lost.
            self.api('users',{'subject':subject,'name':'Benchmark user','role':'user','active':True})
            self.api('budgets',{'subject':subject,'monthly_usd':10000})
            key=self.api('keys',{'subject':subject,'name':'benchmark','expiresIn':'2h'},'POST')['key']
            self.callers.append((subject,key));self.summary['completed_mock_requests'][subject]=0
        self.save()

    def restart(self,payload):
        fixture.run('rm','--force',lab.POD+'-gateway-sso')
        fixture.run('secret','rm',lab.secret_name('gateway-sso.json'))
        lab.write(lab.STATE,'gateway-sso.json',payload.decode())
        fixture.secret('gateway-sso.json',lab.STATE/'gateway-sso.json')
        fixture.start_gateway(self.image,telemetry=self.telemetry,service_name=self.service_name)
        lab.wait_url('https://localhost:28443/login')

    def phase(self,phase):
        config=json.loads(self.original)
        edge=next(chain for chain in config['filter_chains'] if chain['name']=='edge')
        limiter=next(f for f in edge['filters'] if f['filter']=='rate_limit')
        limiter.update(rate=phase['rate_limit_rps'],burst=phase['rate_limit_rps']*4)
        self.restart(json.dumps(config).encode())
        self.phase_started=datetime.datetime.now(datetime.timezone.utc).isoformat()
        print('Phase: '+phase['name'],flush=True)

    def guard(self):
        if Path('/proc/meminfo').exists():
            memory_text=Path('/proc/meminfo').read_text()
        else:
            # Podman machine is the Linux host whose memory the services consume.
            memory_text=subprocess.run(['podman','machine','ssh','cat','/proc/meminfo'],
                check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10).stdout.decode()
        memory={line.split(':')[0]:int(line.split()[1]) for line in memory_text.splitlines()
                if line.startswith(('MemTotal:','MemAvailable:'))}
        if memory['MemAvailable']<memory['MemTotal']*.1:
            raise SafetyStop('Local Linux host memory safety threshold reached: require at least 10% available')
        if Path('/proc/meminfo').exists():
            disk=os.statvfs('/var')
            available,total=disk.f_bavail,disk.f_blocks
        else:
            raw=subprocess.run(['podman','machine','ssh','df','-Pk','/var'],
                check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10).stdout.decode()
            fields=raw.splitlines()[-1].split()
            if len(fields)!=6:raise RuntimeError('Cannot determine local VM disk capacity')
            total,available=int(fields[1]),int(fields[3])
        if total<=0 or available<total*.3:
            raise SafetyStop('Local Linux host disk safety threshold reached: require at least 30% available')
        for suffix in ('gateway-sso','maas','metering-sso','provider','db'):
            info=json.loads(fixture.run('inspect',lab.POD+'-'+suffix))[0]
            if not info['State']['Running'] or info['State'].get('OOMKilled'):
                raise SafetyStop('Fixture service stopped or exceeded memory')

    def observe(self):
        self.guard()
        names=[lab.POD+'-'+suffix for suffix in ('gateway-sso','maas','metering-sso','provider','db')]
        stats=json.loads(fixture.run('stats','--no-stream','--format','json',*names))
        with (self.output/'resources.jsonl').open('a') as stream:
            stream.write(json.dumps({'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'containers':stats})+'\n')
        self.last_sample=time.monotonic()

    @contextmanager
    def transport(self):
        with bench.transport.MockTransport('https://localhost:28443',lab.STATE/'ca.pem',
                [key for _,key in self.callers],self.model) as request:yield request

    def persist(self,phase,result):
        bench.RhelController.persist(self,phase,result)
        if not self.telemetry:return
        started=datetime.datetime.fromisoformat(self.phase_started).timestamp()
        metric_url='http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({
            'query':'timestamp(praxis_http_requests_total{cluster="authenticated-openai"})'})
        trace_url='http://localhost:3200/api/search?'+urllib.parse.urlencode({
            'tags':'service.name='+self.service_name,'start':int(started),'end':int(time.time())+1,'limit':20})
        for _ in range(25):
            metrics=signals.query(metric_url) or {}
            traces=signals.query(trace_url) or {}
            rows=metrics.get('data',{}).get('result',[])
            fresh_metric=any(float(row['value'][1])>=started for row in rows)
            fresh_trace=any(int(trace.get('startTimeUnixNano','0'))/1e9>=started for trace in traces.get('traces',[]))
            if fresh_metric and fresh_trace:
                self.summary['phases'][-1]['telemetry_verified']=True;self.save();return
            time.sleep(2)
        raise RuntimeError('Fresh inference telemetry missing after gateway restart')

    def verify(self):
        for subject,count in self.summary['completed_mock_requests'].items():
            # Subjects are exclusively generated here from a UUID and an integer.
            query=("SELECT count(*),coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0),"
                   "count(cost_usd),coalesce(sum(cost_usd),0) FROM usage_events WHERE username='"+subject+"';")
            for _ in range(30):
                fields=fixture.run('exec',lab.POD+'-db','psql','-U','keycloak','-d','metering_sso',
                    '-At','-v','ON_ERROR_STOP=1','-c',query).decode().strip().split('|')
                if tuple(map(int,fields[:4]))==(count,2*count,3*count,count) and Decimal(fields[4])==self.request_cost*count:break
                time.sleep(1)
            else:return False
        self.summary['ledger_verified']=True;self.save();return True

    def cleanup(self):
        errors=[]
        try:
            for subject in self.created:
                for route,body,method in [('users',{'subject':subject,'active':False},'PUT'),
                                          ('key-revocations',{'subject':subject},'POST')]:
                    try:self.api(route,body,method)
                    except Exception:errors.append('account-retirement')
            if self.original is not None:
                try:self.restart(self.original)
                except Exception:errors.append('gateway-restoration')
            for name in self.secrets:
                try:fixture.run('secret','rm',lab.secret_name(name))
                except Exception:errors.append('secret-removal')
            if self.output.exists():lab.write(self.output,'cleanup.json',{'complete':not errors,'errors':errors})
            return not errors
        finally:
            if self.lock is not None:os.close(self.lock)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--praxis-image',required=True)
    parser.add_argument('--model',help='Configured mock alias; default first mock profile model or legacy fixture')
    parser.add_argument('--duration',type=int,default=600)
    parser.add_argument('--offered-rps',type=float,default=30)
    args=parser.parse_args();os.umask(0o077)
    stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_:stop.set())
    plan=json.loads((HERE/'traffic-patterns.json').read_text())
    controller=LocalController(args.praxis_image,args.model)
    controller.guard() # Check capacity before creating users or changing gateway policy.
    bench.run_plan(controller,plan,duration=args.duration,offered_rps=args.offered_rps,stop=stop)
    print('PASS: six phases, exact ledger/USD reconciliation and cleanup; private report: '+str(controller.output))

if __name__=='__main__':
    try:main()
    except SafetyStop as error:raise SystemExit(str(error)) from None
    except Exception as error:raise SystemExit(type(error).__name__+': local benchmark failed; inspect private reports') from None
