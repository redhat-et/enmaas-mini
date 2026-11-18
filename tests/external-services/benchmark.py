#!/usr/bin/env python3
"""Mock-only RHEL scenario runner with restoration and independent ledger checks."""
import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import datetime
import fcntl
import importlib.machinery
import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.request
import uuid
from workload_profiles import persona, client_for_api, directory_tags, workload, budget_profile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
DEPLOY=Path('/etc/praxis-pricetag')


def load(path,name):
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    spec=importlib.util.spec_from_loader(name,loader)
    module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    return module

scheduler=load(HERE/'scenarios.py','lab_scenarios')
transport=load(HERE/'transport.py','lab_transport')


def validate_plan(plan,duration,offered_rps,timeout):
    if not isinstance(plan,dict) or plan.get('mock_only') is not True:
        raise ValueError('only explicit mock plans are supported')
    phases=plan.get('phases')
    if not isinstance(phases,list) or not 1 <= len(phases) <= 12:
        raise ValueError('require 1–12 phases')
    names=set()
    for phase in phases:
        if not isinstance(phase,dict) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}',str(phase.get('name',''))):
            raise ValueError('invalid phase name')
        if phase['name'] in names: raise ValueError('duplicate phase name')
        names.add(phase['name'])
        users,lanes,rate=(phase.get(k) for k in ('users','streams_per_user','rate_limit_rps'))
        if (any(type(v) is not int for v in (users,lanes,rate)) or not 1<=users<=100
                or not 1<=lanes<=32 or users*lanes>512 or not 1<=rate<=2000):
            raise ValueError('invalid phase resource bounds')
        offered=phase.get('offered_rps',offered_rps)
        if type(offered) not in (int,float) or not math.isfinite(offered) or not 0<offered<=2000:
            raise ValueError('invalid phase offered rate')
        active,idle=phase.get('active_seconds'),phase.get('idle_seconds')
        if (active is None)!=(idle is None) or (active is not None and any(
                not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<v<=3600 for v in (active,idle))):
            raise ValueError('invalid burst durations')
    for value,maximum in ((duration,3600),(offered_rps,2000),(timeout,60)):
        if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=maximum:
            raise ValueError('invalid duration, offered rate or request timeout')


def run_plan(controller,plan,*,duration=600,offered_rps=100,timeout=30,stop=None,cycles=1):
    """Service operations are injected so cleanup can be tested without root."""
    validate_plan(plan,duration,offered_rps,timeout)
    if type(cycles) is not int or not 1<=cycles<=12:raise ValueError('cycles must be 1–12')
    planned_seconds=cycles*len(plan['phases'])*duration
    if planned_seconds>20*3600:raise ValueError('scheduled load must not exceed 20 hours')
    controller.key_lifetime=str(math.ceil(planned_seconds/3600)+1)+'h'
    stop=stop if stop is not None else threading.Event()
    try:
        if stop.is_set(): raise InterruptedError('benchmark cancelled')
        controller.prepare(max(p['users'] for p in plan['phases']))
        for phase in (dict(phase,cycle=cycle,offered_rps=phase.get('offered_rps',offered_rps)) for cycle in range(1,cycles+1) for phase in plan['phases']):
            if stop.is_set(): raise InterruptedError('benchmark cancelled')
            controller.phase(phase)
            controller.observe()
            with controller.transport() as request, ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(scheduler.run_phase,request,users=phase['users'],lanes=phase['streams_per_user'],
                    duration=duration,offered_rps=phase.get('offered_rps',offered_rps),timeout=timeout,stop=stop,
                    active=phase.get('active_seconds'),idle=phase.get('idle_seconds'))
                try:
                    while not future.done():
                        try:
                            future.result(timeout=.5)
                        except FutureTimeout:
                            if not stop.is_set():
                                controller.guard()
                                if controller.sample_due(): controller.observe()
                except BaseException:
                    stop.set()
                    raise
                finally:
                    # Drain bounded in-flight requests before restoring policies.
                    result=future.result()
                    controller.persist(phase,result)
            if result['interrupted'] or result['errors']:
                raise InterruptedError('benchmark stopped; inspect private report')
            if result['statuses'].get('200',0)==0:
                raise RuntimeError('phase completed without successful mock inference')
            if phase['name']=='rate_limited' and result['statuses'].get('429',0)==0:
                raise RuntimeError('rate-limited phase did not exercise denial; adjust offered load')
            controller.observe()
        if getattr(controller,'budget_mix',False):
            verify_budget_mix(controller.summary)
            controller.save()
        if not controller.verify(): raise RuntimeError('ledger reconciliation failed')
    finally:
        if not controller.cleanup(): raise RuntimeError('cleanup incomplete; inspect private recovery files')


def verify_budget_mix(summary):
    """Do not call a short run or an unenforced policy a successful quota test."""
    subjects=list(summary['completed_mock_requests'])
    denied=set()
    counts=dict.fromkeys(subjects,0)
    for phase in summary['phases']:
        for index,count in enumerate(phase.get('per_user_budget_denials',[])):
            counts[subjects[index]]+=count
            if count:denied.add(summary['synthetic_personas'][subjects[index]]['account_type'])
    if denied!={'person','bot'}:
        raise RuntimeError('Budget mix did not demonstrate both person and bot denials; check enforcement, prices, exceptions and run duration')
    summary['expected_budget_denials']=counts
    summary['budget_mix_verified']=True


def managed_command(argv, *, timeout, stop=None, stdout=None, stderr=None):
    """Bound a private process group; cancel descendants as well as the wrapper."""
    process=subprocess.Popen(argv,stdout=stdout,stderr=stderr,start_new_session=True,cwd='/tmp')
    deadline=time.monotonic()+timeout
    try:
        while process.poll() is None:
            if stop is not None and stop.is_set(): raise InterruptedError('operation cancelled')
            if time.monotonic()>=deadline: raise TimeoutError('operation deadline exceeded')
            try: process.wait(timeout=.1)
            except subprocess.TimeoutExpired: pass
        return process.returncode
    finally:
        if process.poll() is None:
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()


class RhelController:
    def __init__(self,stop=None,database_name='metering'):
        self.database_name=database_name_checked(database_name)
        self.stop=stop
        self.bench=load(ROOT/'scripts/pricetag/benchmark','lab_existing_benchmark')
        self.perf=load(ROOT/'scripts/pricetag/perf','lab_existing_perf')
        self.observer=load(ROOT/'scripts/pricetag/observe','lab_existing_observer')
        self.admin=load(ROOT/'scripts/pricetag/metering_admin.py','lab_metering_admin')
        self.name='perf-'+uuid.uuid4().hex[:12]
        self.output=Path('/root/pricetag-admin')/('scenario-'+self.name)
        self.lock=None;self.original=None;self.provisioned=False;self.prefix=None
        self.created=[];self.callers=[];self.profiles=[]
        self.summary={'schema':'enmaas-mock-counts-v1','mock_only':True,
                      'generator_on_gateway_host':True,'connection_policy':'new TLS connection per request',
                      'completed_mock_requests':{},'phases':[]}
        self.last_sample=0;self.restarts=None

    def prepare(self,count):
        self.bench.require_mock(DEPLOY)
        lockdir=Path('/root/pricetag-provider-inputs');lockdir.mkdir(mode=0o700,exist_ok=True)
        self.lock=os.open(lockdir/'.setup.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.bench.require_mock(DEPLOY)  # Revalidate after acquiring the shared mutation lock.
        self.output.mkdir(mode=0o700)
        print('Private benchmark results: '+str(self.output),flush=True)
        owner=pwd.getpwnam('praxis-svc')
        self.prefix=['sudo','-u','praxis-svc','env',f'XDG_RUNTIME_DIR=/run/user/{owner.pw_uid}',
                     f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{owner.pw_uid}/bus']
        models=json.loads((DEPLOY/'models.json').read_text())
        if not models: raise ValueError('Mock catalog is empty')
        rates={model['id']:self.price_preflight(model['model']) for model in models}
        self.model=models[0]['id']
        self.config=DEPLOY/'gateway/gateway.json'
        self.original=self.config.read_bytes()
        (self.output/'gateway-original.json').write_bytes(self.original)
        self.summary.update(models=list(rates),expected_usd_per_user_request={},synthetic_personas={},expected_tokens_per_request={},workload_profiles=[])
        self.provisioned=True
        for index in range(count):
            if self.stop is not None and self.stop.is_set(): raise InterruptedError('benchmark cancelled')
            subject=self.name+'-'+str(index)+'@example.test'
            profile=budget_profile(index,mixed=getattr(self,'budget_mix',False));model=models[index % len(models)];work=workload(index)
            api=model.get('apis',['openai'])[0]
            if api not in ('openai','anthropic'): raise ValueError('Unsupported mock protocol')
            self.created.append(subject)
            # Persist attempted subjects before writes, including uncertain replies.
            self.summary['attempted_subjects']=list(self.created);self.save()
            user=self.admin.request('PUT','users',{'subject':subject,'name':profile['name'],'role':'user','active':True})
            self.patch_directory(user['user_id'],directory_tags(profile,subject))
            self.admin.request('PUT','budgets',{'subject':subject,'monthly_usd':profile['monthly_usd']})
            key=self.admin.request('POST','keys',{'subject':subject,'name':'mock benchmark','expiresIn':getattr(self,'key_lifetime','2h')})['key']
            self.callers.append((subject,key));self.profiles.append({'model':model['id'],'api':api,'workload':work})
            self.summary['workload_profiles']=list(self.profiles)
            quantities=[work['input_tokens'],work['output_tokens']]
            self.summary['expected_tokens_per_request'][subject]=quantities
            self.summary['completed_mock_requests'][subject]=0
            self.summary['expected_usd_per_user_request'][subject]=str(self.price_preflight(model['model'],quantities))
            self.summary['synthetic_personas'][subject]=dict(profile,model=model['id'],user_agent=client_for_api(index,api))

        self.save()

    def patch_directory(self,identity,tags):
        if str(uuid.UUID(identity)) != identity: raise ValueError('Invalid directory UUID')
        token=(DEPLOY/'partner-secret').read_text().strip()
        if not token: raise ValueError('Missing private partner credential')
        request=urllib.request.Request('http://127.0.0.1:18087/api/v1/users/'+identity,
            json.dumps({'tags':tags}).encode(),method='PATCH',
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),self.admin.NoRedirect())
        with opener.open(request,timeout=30) as response:
            raw=response.read(65537)
            if len(raw)>65536 or json.loads(raw).get('user_id') != identity:
                raise ValueError('Unexpected directory response')

    def price_preflight(self,model,tokens=None):
        pricing=SimpleNamespace(database='pricetag-db',database_name=self.database_name,service_user='praxis-svc')
        try:
            return self.perf.mock_request_cost(pricing,model) if tokens is None else self.perf.mock_request_cost(pricing,model,tokens)
        except Exception:
            # Never include database output, connection details or credentials.
            self.summary.update(failure_stage='mock_price_preflight',
                action='Check ledger access and one explicit nonnegative mock model price before retrying.')
            self.save()
            print('Mock price preflight failed; check ledger access and the explicit mock model price.',flush=True)
            raise RuntimeError('mock price preflight failed') from None

    def replace_config(self,payload):
        # Preserve owner/mode/SELinux metadata on the managed file.
        stat=self.config.stat()
        fd,name=tempfile.mkstemp(prefix='.scenario-',dir=self.config.parent)
        try:
            with os.fdopen(fd,'wb') as output:
                output.write(payload);output.flush();os.fsync(output.fileno())
            os.chown(name,stat.st_uid,stat.st_gid)
            shutil.copystat(self.config,name)
            os.replace(name,self.config)
        finally:
            if os.path.exists(name): os.unlink(name)

    def restart(self):
        code=managed_command(self.prefix+['systemctl','--user','restart','pricetag-gateway.service'],timeout=120)
        if code: raise RuntimeError('gateway restart failed')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),self.admin.NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(DEPLOY/'ca.pem'))))
        for _ in range(30):
            try:
                with opener.open('https://localhost:8443/v1/models',timeout=2) as reply:
                    if reply.status==200: return
            except OSError: pass
            time.sleep(1)
        raise RuntimeError('gateway readiness failed')

    def phase(self,phase):
        config=json.loads(self.original)
        edge=next(c for c in config['filter_chains'] if c['name']=='edge')
        limiter=next(f for f in edge['filters'] if f['filter']=='rate_limit')
        limiter.update(rate=phase['rate_limit_rps'],burst=phase['rate_limit_rps']*4)
        self.replace_config((json.dumps(config,indent=2)+'\n').encode())
        self.restart();self.restarts=None
        self.phase_started=datetime.datetime.now(datetime.timezone.utc).isoformat()
        print('Phase: '+phase['name'],flush=True)

    def guard(self):
        memory={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith(('MemTotal:','MemAvailable:'))}
        disk=os.statvfs('/var')
        if memory['MemAvailable']<memory['MemTotal']*.1 or disk.f_bavail<disk.f_blocks*.3:
            raise RuntimeError('memory or disk safety threshold reached')

    def sample_due(self): return time.monotonic()-self.last_sample>=10

    def observe(self):
        self.guard()
        record=self.observer.sample(self.prefix,self.database_name)
        with (self.output/'observe.jsonl').open('a') as output: output.write(json.dumps(record)+'\n')
        restarts=self.bench.check_sample(record)
        if self.restarts is not None and restarts!=self.restarts:
            raise RuntimeError('service restarted during traffic')
        self.restarts=restarts;self.last_sample=time.monotonic()

    @contextmanager
    def transport(self):
        with transport.MockTransport('https://localhost:8443',DEPLOY/'ca.pem',
                [key for _,key in self.callers],self.model,
                user_agents=[client_for_api(i,self.profiles[i]['api']) for i in range(len(self.callers))],
                profiles=self.profiles,allow_budget_denials=getattr(self,'budget_mix',False)) as client: yield client

    def save(self):
        path=self.output/'summary.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.summary,indent=2)+'\n');temporary.replace(path)

    def persist(self,phase,result):
        self.summary['phases'].append({'name':phase['name'],'cycle':phase.get('cycle',1),'offered_rps':phase.get('offered_rps'),'rate_limit_rps':phase['rate_limit_rps'],
            'started_utc':self.phase_started,'ended_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),**result})
        for index,count in enumerate(result['per_user_successes']):
            self.summary['completed_mock_requests'][self.callers[index][0]]+=count
        self.save()

    def verify(self):
        with (self.output/'reconciliation.log').open('w') as log:
            code=managed_command([sys.executable,str(ROOT/'scripts/pricetag/perf'),'verify','--report',
                str(self.output/'summary.json'),'--database','pricetag-db','--service-user','praxis-svc',
                '--database-name',self.database_name],
                stdout=log,stderr=log,timeout=120,stop=self.stop)
        self.summary['ledger_verified']=code==0;self.save()
        return code==0

    def cleanup(self):
        errors=[]
        try:
            if self.original is not None:
                try: self.replace_config(self.original);self.restart()
                except Exception as error: errors.append('restore:'+type(error).__name__)
            if self.provisioned:
                subjects=set(getattr(self,'created',[]))
                try:
                    for user in self.admin.request('GET','users'):
                        subjects.update(s for s in user.get('logins',[user.get('user_id','')])
                                        if s.startswith(self.name+'-'))
                except Exception as error: errors.append('list:'+type(error).__name__)
                for subject in sorted(subjects):
                    if not subject.startswith(self.name+'-'):
                        errors.append('foreign-cleanup-identity');continue
                    for method,resource,payload in (
                        ('PUT','users',{'subject':subject,'active':False}),
                        ('POST','key-revocations',{'subject':subject})):
                        try: self.admin.request(method,resource,payload)
                        except Exception as error: errors.append('retire:'+type(error).__name__)
            if self.output.exists():
                (self.output/'cleanup.json').write_text(json.dumps({'complete':not errors,'errors':errors})+'\n')
            return not errors
        finally:
            if self.lock is not None: os.close(self.lock);self.lock=None


def database_name_checked(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',value):
        raise ValueError('invalid ledger database name')
    return value


def launch_detached(args, plan):
    """Submit one mock-only run to systemd; submission is not benchmark success."""
    validate_plan(plan,args.duration,args.offered_rps,args.timeout)
    database_name=database_name_checked(args.database_name)
    load(ROOT/'scripts/pricetag/benchmark','lab_detached_guard').require_mock(DEPLOY)
    unit='enmaas-mock-benchmark-'+uuid.uuid4().hex[:12]+'.service'
    # Print before submission: a timeout may mean systemd accepted the job.
    print('Benchmark unit: '+unit,flush=True)
    command=['systemd-run','--unit='+unit,'--property=Type=exec',
             '--property=RemainAfterExit=yes','--property=UMask=0077',
             '--property=KillMode=mixed','--property=TimeoutStopSec=180',
             '--property=StandardOutput=journal','--property=StandardError=journal',
             '--working-directory='+str(ROOT),'--',sys.executable,str(Path(__file__).resolve()),
             '--duration',str(args.duration),'--offered-rps',str(args.offered_rps),
             '--timeout',str(args.timeout),'--cycles',str(getattr(args,'cycles',1)),'--scenario',getattr(args,'scenario','traffic'),'--database-name',database_name]
    if getattr(args,'budget_mix',False):command.append('--budget-mix')
    try:
        result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,timeout=30)
        if result.returncode:raise OSError('submission failed')
    except (OSError,subprocess.TimeoutExpired):
        raise RuntimeError('Benchmark submission failed or uncertain; inspect the printed unit before retrying') from None
    print('Submitted; inspect with: sudo systemctl status '+unit+' --no-pager',flush=True)
    print('Progress/result path: sudo journalctl -u '+unit+' --no-pager',flush=True)
    print('Request graceful cancellation: sudo systemctl stop '+unit,flush=True)
    return unit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--detach',action='store_true',help='run under systemd so SSH disconnection does not stop the benchmark')
    parser.add_argument('--budget-mix',action='store_true',help='Use varied per-user allowances including deliberately low budgets; not a capacity test')
    parser.add_argument('--duration',type=int,default=600,help='seconds per phase, default 600')
    parser.add_argument('--offered-rps',type=float,default=100,help='fallback target for phases without their own offered_rps; sustained phases set explicit targets')
    parser.add_argument('--timeout',type=float,default=30)
    parser.add_argument('--cycles',type=int,default=1)
    parser.add_argument('--scenario',choices=('traffic','sustained','budgets'),default='traffic')
    parser.add_argument('--database-name',default='metering',help='active metering database; specify the name for an upgrade clone')
    args=parser.parse_args()
    if os.geteuid()!=0 or not Path('/proc/meminfo').exists():
        parser.error('run with sudo on the dedicated mock gateway RHEL VM')
    os.umask(0o077)
    stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM): signal.signal(number,lambda *_:stop.set())
    plan=json.loads((HERE/(args.scenario+'-patterns.json')).read_text())
    if args.detach:
        launch_detached(args,plan)
        return
    controller=RhelController(stop,args.database_name)
    controller.budget_mix=args.budget_mix or args.scenario=='budgets'
    run_plan(controller,plan,duration=args.duration,offered_rps=args.offered_rps,timeout=args.timeout,stop=stop,cycles=args.cycles)
    print('PASS: scenario phases and ledger verification; policies restored and test keys retired')

if __name__=='__main__':
    try: main()
    except Exception as error:
        raise SystemExit(type(error).__name__+': benchmark stopped; inspect private results and cleanup status') from None
