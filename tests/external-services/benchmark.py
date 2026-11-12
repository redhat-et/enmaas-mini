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
        active,idle=phase.get('active_seconds'),phase.get('idle_seconds')
        if (active is None)!=(idle is None) or (active is not None and any(
                not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<v<=3600 for v in (active,idle))):
            raise ValueError('invalid burst durations')
    for value,maximum in ((duration,3600),(offered_rps,2000),(timeout,60)):
        if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=maximum:
            raise ValueError('invalid duration, offered rate or request timeout')


def run_plan(controller,plan,*,duration=600,offered_rps=100,timeout=30,stop=None):
    """Service operations are injected so cleanup can be tested without root."""
    validate_plan(plan,duration,offered_rps,timeout)
    stop=stop if stop is not None else threading.Event()
    try:
        if stop.is_set(): raise InterruptedError('benchmark cancelled')
        controller.prepare(max(p['users'] for p in plan['phases']))
        for phase in plan['phases']:
            if stop.is_set(): raise InterruptedError('benchmark cancelled')
            controller.phase(phase)
            controller.observe()
            with controller.transport() as request, ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(scheduler.run_phase,request,users=phase['users'],lanes=phase['streams_per_user'],
                    duration=duration,offered_rps=offered_rps,timeout=timeout,stop=stop,
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
        if not controller.verify(): raise RuntimeError('ledger reconciliation failed')
    finally:
        if not controller.cleanup(): raise RuntimeError('cleanup incomplete; inspect private recovery files')


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
    def __init__(self,stop=None):
        self.stop=stop
        self.bench=load(ROOT/'scripts/pricetag/benchmark','lab_existing_benchmark')
        self.perf=load(ROOT/'scripts/pricetag/perf','lab_existing_perf')
        self.observer=load(ROOT/'scripts/pricetag/observe','lab_existing_observer')
        self.admin=load(ROOT/'scripts/pricetag/metering_admin.py','lab_metering_admin')
        self.name='perf-'+uuid.uuid4().hex[:12]
        self.output=Path('/root/pricetag-admin')/('scenario-'+self.name)
        self.lock=None;self.original=None;self.provisioned=False;self.prefix=None
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
        self.config=DEPLOY/'gateway/gateway.json'
        self.original=self.config.read_bytes()
        (self.output/'gateway-original.json').write_bytes(self.original)
        model=json.loads((DEPLOY/'models.json').read_text())[0]
        self.model=model['id']
        pricing=SimpleNamespace(database='pricetag-db',database_name='metering',service_user='praxis-svc')
        cost=self.perf.mock_request_cost(pricing,model['model'])
        self.summary.update(model=self.model,expected_usd_per_request=str(cost))
        self.provisioned=True  # Includes an account created before key issuance failed.
        code=managed_command([sys.executable,str(ROOT/'scripts/pricetag/perf'),'users','--tokens',str(self.output/'keys'),
            '--prefix',self.name,'--count',str(count),'--monthly-usd','10000'],timeout=900,stop=self.stop)
        if code: raise RuntimeError('test identity provisioning failed')
        self.callers=self.perf.identities(self.output/'keys')
        self.summary['completed_mock_requests']={subject:0 for subject,_ in self.callers}
        self.save()

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
        record=self.observer.sample(self.prefix)
        with (self.output/'observe.jsonl').open('a') as output: output.write(json.dumps(record)+'\n')
        restarts=self.bench.check_sample(record)
        if self.restarts is not None and restarts!=self.restarts:
            raise RuntimeError('service restarted during traffic')
        self.restarts=restarts;self.last_sample=time.monotonic()

    @contextmanager
    def transport(self):
        with transport.MockTransport('https://localhost:8443',DEPLOY/'ca.pem',
                [key for _,key in self.callers],self.model) as client: yield client

    def save(self):
        path=self.output/'summary.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.summary,indent=2)+'\n');temporary.replace(path)

    def persist(self,phase,result):
        self.summary['phases'].append({'name':phase['name'],'rate_limit_rps':phase['rate_limit_rps'],
            'started_utc':self.phase_started,'ended_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),**result})
        for index,count in enumerate(result['per_user_successes']):
            self.summary['completed_mock_requests'][self.callers[index][0]]+=count
        self.save()

    def verify(self):
        with (self.output/'reconciliation.log').open('w') as log:
            code=managed_command([sys.executable,str(ROOT/'scripts/pricetag/perf'),'verify','--report',
                str(self.output/'summary.json'),'--database','pricetag-db','--service-user','praxis-svc'],
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
                try:
                    for user in self.admin.request('GET','users'):
                        subject=user.get('user_id','')
                        if subject.startswith(self.name+'-'):
                            for method,resource,payload in (
                                ('PUT','users',{'subject':subject,'active':False}),
                                ('POST','key-revocations',{'subject':subject})):
                                try: self.admin.request(method,resource,payload)
                                except Exception as error: errors.append('retire:'+type(error).__name__)
                except Exception as error: errors.append('list:'+type(error).__name__)
            if self.output.exists():
                (self.output/'cleanup.json').write_text(json.dumps({'complete':not errors,'errors':errors})+'\n')
            return not errors
        finally:
            if self.lock is not None: os.close(self.lock);self.lock=None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration',type=int,default=600,help='seconds per phase, default 600')
    parser.add_argument('--offered-rps',type=float,default=100,help='global admission target while active')
    parser.add_argument('--timeout',type=float,default=30)
    args=parser.parse_args()
    if os.geteuid()!=0 or not Path('/proc/meminfo').exists():
        parser.error('run with sudo on the dedicated mock gateway RHEL VM')
    os.umask(0o077)
    stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM): signal.signal(number,lambda *_:stop.set())
    plan=json.loads((HERE/'traffic-patterns.json').read_text())
    run_plan(RhelController(stop),plan,duration=args.duration,offered_rps=args.offered_rps,timeout=args.timeout,stop=stop)
    print('PASS: scenario phases and ledger verification; policies restored and test keys retired')

if __name__=='__main__':
    try: main()
    except Exception as error:
        raise SystemExit(type(error).__name__+': benchmark stopped; inspect private results and cleanup status') from None
