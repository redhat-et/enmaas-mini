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
import shlex
import threading
import time
import uuid
import local_probe
from workload_profiles import persona, client_for_api, directory_tags, workload, budget_profile
from workload_signals import payloads
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
host_metrics=load('local-host-metrics.py','benchmark_host_metrics')
phase_metrics=load('phase-metrics.py','benchmark_phase_metrics')
component_metrics=load('container-metrics.py','benchmark_component_metrics')

class SafetyStop(RuntimeError):
    """Fixed, nonsecret diagnostics that can be shown without private reports."""

class LocalController:
    save=bench.RhelController.save
    sample_due=bench.RhelController.sample_due

    def __init__(self,image,model=None,min_disk_percent=30,min_disk_gib=8):
        if type(min_disk_percent) is not int or not 10<=min_disk_percent<=50 or type(min_disk_gib) is not int or not 4<=min_disk_gib<=1000:
            raise ValueError('Invalid disk safety reserve')
        self.min_disk_percent=min_disk_percent;self.min_disk_gib=min_disk_gib
        self.model=model or 'fixture';self.request_cost=Decimal('0.000008')
        self.image=image;self.original=None;self.lock=None;self.callers=[];self.created=[];self.secrets=[]
        self.telemetry=False;self.service_name='enmaas-gateway'
        self.profiles=None;self.costs={};self.tokens={};self.model_override=model is not None
        self.name='lab-run-'+uuid.uuid4().hex
        self.output=lab.STATE/self.name
        self.last_sample=0
        self.last_guard=float("-inf")
        self.summary={'mock_only':True,'generator':'local host','connection_policy':'new TLS connection per request',
                      'completed_mock_requests':{},'phases':[]}

    def api(self,route,body,method='PUT'):
        raw=fixture.run('run','--rm','--interactive','--pod',lab.POD,'--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',
            '--secret',lab.secret_name(self.name+'-management')+',target=/fixture/management,mode=0444',
            '--secret',lab.secret_name('gateway-sso-partner')+',target=/fixture/partner,mode=0444',
            '--secret',lab.secret_name(self.name+'-client')+',target=/fixture/client.py,mode=0444',
            fixture.prepare.PYTHON,'python3','-B','/fixture/client.py',
            input=json.dumps({'route':route,'body':body,'method':method}).encode())
        return json.loads(raw)

    def prepare(self,count):
        self.lock=os.open(lab.STATE/'.benchmark.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        lab.check_port_bindings()
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
            candidates = document['models']
            if self.model_override:
                candidates = [m for m in candidates if m['id'] == self.model]
            if not candidates: raise ValueError('Select a configured mock alias')
            self.profiles=[]
            rates={}
            for selected in candidates:
                upstream=selected['model']
                if not re.fullmatch(r'[A-Za-z0-9_./-]+',upstream):raise ValueError('Invalid mock upstream ID')
                query="SELECT input_cost_per_mtok,output_cost_per_mtok FROM model_pricing WHERE model='"+upstream+"';"
                row=fixture.run('exec',lab.POD+'-db','psql','-U','keycloak','-d','metering_sso',
                                '-At','-v','ON_ERROR_STOP=1','-c',query).decode().strip().split('|')
                if len(row)!=2:raise ValueError('Mock model has no explicit imported price')
                rates[selected['id']]=tuple(Decimal(value) for value in row)
                if any(not value.is_finite() or value<0 for value in rates[selected['id']]):raise ValueError('Invalid imported price')
            for index in range(count):
                selected=candidates[index % len(candidates)]
                work=workload(index)
                self.profiles.append({'model':selected['id'], 'api':selected['apis'][0],'workload':work})
                subject=self.name+'-'+str(index)+'@example.test'
                incoming,outgoing=work['input_tokens'],work['output_tokens']
                self.tokens[subject]=(incoming,outgoing)
                price=rates[selected['id']]
                self.costs[subject]=((price[0]*incoming+price[1]*outgoing)/1000000).quantize(Decimal('0.00000001'),rounding=ROUND_HALF_UP)
            self.model=candidates[0]['id']
            self.request_cost=self.costs[self.name+'-0@example.test']
            self.summary['models']=list(rates)
            self.summary['expected_tokens_per_request']={k:list(v) for k,v in self.tokens.items()}
            self.summary['workload_profiles']=self.profiles
            self.summary['expected_usd_per_user_request']={k:str(v) for k,v in self.costs.items()}

        else:
            if self.model!='fixture':raise ValueError('Legacy mock only serves fixture')
            self.api('prices',{'model':'fixture','provider':'vllm','input_cost_per_mtok':1,'output_cost_per_mtok':2,
                               'cache_write_cost_per_mtok':0,'cache_read_cost_per_mtok':0})
        self.provision_users(count)

    def provision_users(self,count):
        if type(count) is not int or not 1<=count<=2500:raise ValueError('Invalid population bound')
        for index in range(len(self.callers),count):
            subject=self.name+'-'+str(index)+'@example.test'
            self.created.append(subject) # Cleanup also covers a committed write whose reply was lost.
            user=self.api('users',{'subject':subject,'name':persona(index)['name'],'role':'user','active':True})
            profile=budget_profile(index,mixed=getattr(self,'budget_mix',False))
            self.api('profile',{'user_id':user['user_id'],'tags':directory_tags(profile,subject)},'PATCH')
            self.summary.setdefault('synthetic_personas', {})[subject] = dict(profile,
                user_agent=client_for_api(index,self.profiles[index]['api'] if self.profiles else 'openai'),
                model=self.profiles[index]['model'] if self.profiles else self.model)
            self.api('budgets',{'subject':subject,'monthly_usd':profile['monthly_usd']})
            key=self.api('keys',{'subject':subject,'name':'benchmark','expiresIn':getattr(self,'key_lifetime','2h')},'POST')['key']
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
        self.current_phase=phase
        print('Phase: '+phase['name'],flush=True)

    def guard(self):
        if time.monotonic()-self.last_guard<5:return
        if Path('/proc/meminfo').exists():
            memory_text=Path('/proc/meminfo').read_text()
        else:
            # Podman machine is the Linux host whose memory the services consume.
            memory_text=local_probe.read(['podman','machine','ssh','cat','/proc/meminfo']).stdout.decode()
        memory={line.split(':')[0]:int(line.split()[1]) for line in memory_text.splitlines()
                if line.startswith(('MemTotal:','MemAvailable:'))}
        if memory['MemAvailable']<memory['MemTotal']*.1:
            raise SafetyStop('Local Linux host memory safety threshold reached: require at least 10% available')
        if Path('/proc/meminfo').exists():
            disk=os.statvfs('/var')
            available,total=disk.f_bavail,disk.f_blocks
            available_bytes=available*disk.f_frsize
        else:
            raw=local_probe.read(['podman','machine','ssh','df','-Pk','/var']).stdout.decode()
            fields=raw.splitlines()[-1].split()
            if len(fields)!=6:raise RuntimeError('Cannot determine local VM disk capacity')
            total,available=int(fields[1]),int(fields[3])
            available_bytes=available*1024
        if total<=0 or available<total*self.min_disk_percent/100 or available_bytes<self.min_disk_gib*1024**3:
            raise SafetyStop('Local Linux host disk safety threshold reached: require at least '+str(self.min_disk_percent)+'% and '+str(self.min_disk_gib)+' GiB available')
        components=component_metrics.COMPONENTS if self.telemetry else ('gateway-sso','maas','metering-sso','provider','db')
        names=[lab.POD+'-'+suffix for suffix in components]
        records=json.loads(local_probe.read(['podman','inspect',*names]).stdout)
        if len(records)!=len(names):raise SafetyStop('Incomplete fixture health observation')
        for info in records:
            name=info.get('Name','').lstrip('/')
            if name not in names:raise SafetyStop('Unexpected fixture health observation')
            if not info['State']['Running'] or info['State'].get('OOMKilled'):
                raise SafetyStop('Fixture service stopped or exceeded memory: '+name.removeprefix(lab.POD+'-'))
        self.last_guard=time.monotonic()

    def observe(self):
        self.guard()
        if self.telemetry:
            stats=host_metrics.collect()
            signals.send('metrics',phase_metrics.payload(self.current_phase))
        else:
            names=[lab.POD+'-'+suffix for suffix in ('gateway-sso','maas','metering-sso','provider','db')]
            stats=json.loads(fixture.run('stats','--no-stream','--format','json',*names))
        with (self.output/'resources.jsonl').open('a') as stream:
            stream.write(json.dumps({'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'containers':stats})+'\n')
        self.last_sample=time.monotonic()

    @contextmanager
    def transport(self):
        with bench.transport.MockTransport('https://localhost:28443',lab.STATE/'ca.pem',
                [key for _,key in self.callers],self.model,
                user_agents=[client_for_api(i,self.profiles[i]['api'] if self.profiles else 'openai')
                             for i in range(len(self.callers))], profiles=self.profiles,allow_budget_denials=getattr(self,'budget_mix',False)) as request:yield request

    def persist(self,phase,result):
        bench.RhelController.persist(self,phase,result)
        if not self.telemetry:return
        for kind,body in payloads(sum(self.summary['completed_mock_requests'].values())).items():
            signals.send(kind,body)
        started=datetime.datetime.fromisoformat(self.phase_started).timestamp()
        metric_url='http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({
            'query':'timestamp(praxis_http_requests_total{cluster="authenticated-openai"})'})
        trace_url='http://localhost:3200/api/search?'+urllib.parse.urlencode({
            'q':'{resource.service.name = "'+self.service_name+'" && span.enmaas.request.kind = "inference"}','start':int(started),'end':int(time.time())+1,'limit':20})
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
            denied=self.summary.get('expected_budget_denials',{}).get(subject,0)
            if 'expected_budget_denials' in self.summary:
                query=query.replace(' FROM usage_events', ",count(*) FILTER (WHERE status_code=200),count(*) FILTER (WHERE status_code=429 AND prompt_tokens=0 AND completion_tokens=0 AND total_tokens=0 AND cost_usd=0) FROM usage_events")
            incoming,outgoing=self.tokens.get(subject,(2,3))
            for _ in range(30):
                fields=fixture.run('exec',lab.POD+'-db','psql','-U','keycloak','-d','metering_sso',
                    '-At','-v','ON_ERROR_STOP=1','-c',query).decode().strip().split('|')
                if (tuple(map(int,fields[:4]))==(count+denied,incoming*count,outgoing*count,count+denied)
                        and Decimal(fields[4])==self.costs.get(subject,self.request_cost)*count
                        and (len(fields)==5 or list(map(int,fields[5:]))==[count,denied])):break
                time.sleep(1)
            else:return False
        self.summary['ledger_verified']=True;self.save();return True

    def close_management(self):
        """Optional persistent client shutdown before removing mounted secrets."""

    def cleanup(self):
        errors=[]
        try:
            for subject in self.created:
                for route,body,method in [('users',{'subject':subject,'active':False},'PUT'),
                                          ('key-revocations',{'subject':subject},'POST')]:
                    try:self.api(route,body,method)
                    except Exception:errors.append('account-retirement')
            try:self.close_management()
            except Exception:errors.append('management-client-stop')
            if self.original is not None:
                try:self.restart(self.original)
                except Exception:errors.append('gateway-restoration')
            for name in self.secrets:
                try:fixture.run('secret','rm',lab.secret_name(name))
                except Exception:errors.append('secret-removal')
            if self.telemetry:
                try:
                    signals.send('metrics',phase_metrics.payload({'name':'idle'}))
                    self.summary['telemetry_idle_reset']=True
                except Exception:self.summary['telemetry_idle_reset']=False
                if self.output.exists():self.save()
            if self.output.exists():lab.write(self.output,'cleanup.json',{'complete':not errors,'errors':errors})
            return not errors
        finally:
            if self.lock is not None:os.close(self.lock)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--praxis-image',required=True)
    parser.add_argument('--min-free-disk-percent',type=int,default=30)
    parser.add_argument('--min-free-disk-gib',type=int,default=8)
    parser.add_argument('--model',help='Pin one mock alias; default mixes all configured mock aliases across users')
    parser.add_argument('--budget-mix',action='store_true',help='Dashboard scenario: varied per-user budgets, including low-budget people and bots; not a capacity result')
    parser.add_argument('--duration',type=int,default=600)
    parser.add_argument('--cycles',type=int,default=1)
    parser.add_argument('--scenario',choices=('traffic','sustained','budgets'),default='sustained')
    parser.add_argument('--offered-rps',type=float,default=30,help='fallback target for phases without their own offered_rps; sustained phases set explicit targets')
    args=parser.parse_args();os.umask(0o077)
    stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_:stop.set())
    plan=json.loads((HERE/(args.scenario+'-patterns.json')).read_text())
    controller=LocalController(args.praxis_image,args.model,args.min_free_disk_percent,args.min_free_disk_gib)
    controller.budget_mix=args.budget_mix or args.scenario=='budgets'
    print('Private report: '+str(controller.output),flush=True)
    controller.guard() # Check capacity before creating users or changing gateway policy.
    bench.run_plan(controller,plan,duration=args.duration,offered_rps=args.offered_rps,stop=stop,cycles=args.cycles)
    print('PASS: '+str(len(controller.summary['phases']))+' phases, exact ledger/USD reconciliation and cleanup; private report: '+str(controller.output))
    print('export RUN_DIR='+shlex.quote(str(controller.output)))

if __name__=='__main__':
    try:main()
    except SafetyStop as error:raise SystemExit(str(error)) from None
    except Exception as error:raise SystemExit(type(error).__name__+': local benchmark failed; inspect private reports') from None
