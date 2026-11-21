#!/usr/bin/env python3
"""Local-only asynchronous capacity staircase with restoration and safety guards."""
import argparse
import asyncio
import json
import re
from decimal import Decimal
import os
import resource
import signal
import subprocess
import threading
import time

from async_scenarios import run_phase
from async_transport import AsyncTransport
import parallel_generator
from private_api_bridge import PrivateApiBridge
from stress_plan import plan, escalation_stop, select_phases
from stress_signals import payload as stress_payload
from workload_profiles import client_for_api
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('local_benchmark',HERE/'local-benchmark.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
fixture,lab=base.fixture,base.lab


class Controller(base.LocalController):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.bridge=None;self.provider=None;self.provider_memory_mib=512
        self.gateway_memory_mib=None;self.restoring=False;self.original_gateway_memory=None
        self.keep_gateway_running=False;self.last_gateway_setup=None
        self.database_profile=False
        self.generator_image=None;self.generator_workers=1;self.generator_memory_mib=512
        self.bridge_name=lab.POD+'-'+self.name+'-api'

    def prepare(self,count):
        record=json.loads(fixture.run('inspect',lab.POD+'-gateway-sso'))[0]
        self.original_gateway_memory=record['HostConfig']['Memory']
        super().prepare(count)

    def restart(self,payload):
        setup=(payload,self.gateway_memory_mib)
        if self.keep_gateway_running and not self.restoring and setup==self.last_gateway_setup:return
        super().restart(payload)
        memory=(str(self.original_gateway_memory) if self.restoring and self.original_gateway_memory is not None
                else str(self.gateway_memory_mib)+'m' if self.gateway_memory_mib is not None and not self.restoring else None)
        if memory is not None:fixture.run('update','--memory',memory,lab.POD+'-gateway-sso')
        self.last_gateway_setup=setup

    def record_generator(self,phase,snapshot):
        record=dict(phase=phase['name'],sample_unix_seconds=time.time(),**snapshot)
        lab.write(self.output,'generator-latest.json',record)
        with (self.output/'generator.jsonl').open('a') as stream:
            stream.write(json.dumps(record)+'\n')

    def provision_users(self,count):
        # Prepare model/cost metadata for the plan, but add identities by stage.
        super().provision_users(min(count,25))

    def api(self,route,body,method='PUT'):
        if self.bridge is not None and self.bridge.process.poll() is not None:
            self.bridge.close();self.bridge=None
        if self.bridge is None:
            self.bridge=PrivateApiBridge(['podman','run','--rm','--interactive','--name',self.bridge_name,
                '--label',lab.LABEL,'--pod',lab.POD,'--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',
                '--secret',lab.secret_name(self.name+'-management')+',target=/fixture/management,mode=0444',
                '--secret',lab.secret_name('gateway-sso-partner')+',target=/fixture/partner,mode=0444',
                '--secret',lab.secret_name(self.name+'-client')+',target=/fixture/client.py,mode=0444',
                fixture.prepare.PYTHON,'python3','-B','/fixture/client.py','--serve'])
        return self.bridge.call({'route':route,'body':body,'method':method})

    def replace_provider(self,image):
        if self.provider_memory_mib not in (512,1024,2048,4096):raise ValueError('Invalid mock memory limit')
        record=json.loads(fixture.run('inspect',lab.POD+'-provider'))[0]
        if record['Config'].get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE:
            raise ValueError('Unowned provider')
        self.provider=record['Config']['CreateCommand']
        lab.write(self.output,'provider-restore.json',self.provider)
        fixture.run('rm','--force',lab.POD+'-provider')
        fixture.run('run','--detach','--name',lab.POD+'-provider','--label',lab.LABEL,
            '--pod',lab.POD,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--memory',str(self.provider_memory_mib)+'m','--pids-limit','256','--tmpfs','/tmp:rw,noexec,nosuid,size=16m',
            '--secret',lab.secret_name('mock-profiles')+',target=/fixtures/mock-profiles.json,mode=0444',image)

    def close_management(self):
        if self.bridge:self.bridge.close()
        result=subprocess.run(['podman','rm','--force','--ignore',self.bridge_name],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode:raise RuntimeError('Private management container cleanup failed')

    def verify(self):
        """Reconcile the entire bounded run in one grouped read-only query."""
        if not re.fullmatch(r'lab-run-[0-9a-f]{32}',self.name):raise ValueError('Invalid run identity')
        expected=self.summary['completed_mock_requests']
        if any(not re.fullmatch(re.escape(self.name)+r'-[0-9]+@example\.test',subject) for subject in expected):
            raise ValueError('Foreign benchmark identity')
        query=("SELECT username,count(*),coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0),"
               "count(cost_usd),coalesce(sum(cost_usd),0) FROM usage_events WHERE username LIKE '"+
               self.name+"-%' GROUP BY username")
        for _ in range(30):
            raw=fixture.run('exec',lab.POD+'-db','psql','-X','-U','keycloak','-d','metering_sso',
                '-At','-v','ON_ERROR_STOP=1','-c',query).decode()
            rows={}
            for line in raw.splitlines():
                subject,*values=line.split('|')
                if subject in rows or len(values)!=5:raise ValueError('Invalid ledger result')
                rows[subject]=values
            mismatch=set(rows)-set(expected)
            for subject,count in expected.items():
                values=rows.get(subject,['0']*5)
                incoming,outgoing=self.tokens.get(subject,(2,3))
                if tuple(map(int,values[:4]))!=(count,incoming*count,outgoing*count,count) or Decimal(values[4])!=self.costs.get(subject,self.request_cost)*count:
                    mismatch.add(subject)
            self.summary['ledger_mismatch_identities']=len(mismatch)
            if not mismatch:
                self.summary['ledger_verified']=True
                self.summary['ledger_verification_method']='Exact grouped per-user request/token/USD comparison'
                self.save();return True
            time.sleep(1)
        self.summary['ledger_verified']=False;self.save();return False

    def cleanup(self):
        self.restoring=True
        restored=True
        if self.provider:
            try:
                fixture.run('rm','--force',lab.POD+'-provider')
                subprocess.run(self.provider,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except Exception:restored=False
        try:
            clean=super().cleanup()
        finally:
            if self.bridge:self.bridge.close()
            subprocess.run(['podman','rm','--force',self.bridge_name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if not restored:
            lab.write(self.output,'cleanup.json',{'complete':False,'errors':['provider-restoration']})
        return clean and restored


async def exercise(controller,rows,stop):
    stop_reason=None
    for phase in rows:
        if stop.is_set():break
        if stop_reason and phase['name']!='stress_recovery':continue
        await asyncio.to_thread(base.LocalController.provision_users,controller,phase['users'])
        controller.phase(dict(phase,streams_per_user=1,cycle=1))
        await asyncio.sleep(3)
        controller.guard()
        profiler=base.load('database-profile.py','database_profile') if controller.database_profile is True else None
        db_before=await asyncio.to_thread(profiler.snapshot,lab.POD) if profiler else None
        finished=asyncio.Event()
        snapshot={}
        async def observe():
            while not finished.is_set():
                if snapshot:controller.record_generator(phase,dict(snapshot))
                await asyncio.to_thread(controller.observe)
                if snapshot:
                    await asyncio.to_thread(base.signals.send,'metrics',stress_payload(dict(snapshot),phase['concurrency']))
                try:await asyncio.wait_for(finished.wait(),timeout=15)
                except TimeoutError:pass
        sampler=asyncio.create_task(observe())
        workload=None;worker_evidence=[]
        try:
            count=phase['users']
            async with AsyncTransport(lab.url('gateway'),lab.STATE/'ca.pem',
                [key for _,key in controller.callers[:count]],controller.model,
                profiles=controller.profiles[:count],
                user_agents=[client_for_api(i,controller.profiles[i]['api']) for i in range(count)],
                concurrency=phase['concurrency'],
                minimum_seconds=phase['minimum_response_seconds'],
                maximum_seconds=phase['maximum_response_seconds']) as request:
                runner=run_phase;options={}
                if controller.generator_image is not None:
                    runner=parallel_generator.run_phase
                    options=dict(workers=controller.generator_workers,image=controller.generator_image,
                        memory_mib=controller.generator_memory_mib,evidence=worker_evidence)
                workload=asyncio.create_task(runner(request,users=count,concurrency=phase['concurrency'],
                    duration=phase['duration'],offered_rps=phase['offered_rps'],timeout=150,stop=stop,sample=snapshot.update,**options))
                done,_=await asyncio.wait((workload,sampler),return_when=asyncio.FIRST_COMPLETED)
                if sampler in done:
                    workload.cancel()
                    await asyncio.gather(workload,return_exceptions=True)
                    await sampler
                    raise RuntimeError('Resource sampler stopped early')
                result=await workload
            if snapshot:
                await asyncio.to_thread(base.signals.send,'metrics',stress_payload(dict(snapshot),phase['concurrency']))
            controller.persist(phase,result)
            if profiler:
                db_after=await asyncio.to_thread(profiler.snapshot,lab.POD)
                costs=profiler.compare(db_before,db_after)
                lab.write(controller.output,phase['name']+'-database.json',{
                    'phase':phase['name'],'statistics_epoch':db_after['stats_reset'],
                    'scope':'metering database; successful statement execution; category is heuristic',
                    'maximum_scope':'since statistics reset, not phase maximum',
                    'statements':costs})
            reason=escalation_stop(result)
            if reason:
                stop_reason=reason;controller.summary['escalation_stop']=reason;controller.save()
        finally:
            if snapshot:controller.record_generator(phase,dict(snapshot))
            if workload is not None and not workload.done():
                workload.cancel();await asyncio.gather(workload,return_exceptions=True)
            if controller.generator_image is not None:
                lab.write(controller.output,phase['name']+'-workers.json',worker_evidence)
            finished.set()
            await sampler
    return stop_reason


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--praxis-image',required=True)
    parser.add_argument('--provider-image',required=True)
    parser.add_argument('--duration',type=int,default=600)
    parser.add_argument('--database-profile',action='store_true',help='Collect anonymized statement costs per phase; requires preloaded pg_stat_statements')
    parser.add_argument('--profile',choices=('decade','gradual','extended','full','higher'),default='decade',help='Gradual: 10/50/100/150; extended: 100/150/200/250; full: 10/50/100/150/200/250; higher: 500/750/1000 requests/s, then recovery')
    parser.add_argument('--keep-gateway-running',action='store_true',help='Reuse a running gateway between stages with identical policy and limits')
    parser.add_argument('--concurrency',type=int,default=5000)
    parser.add_argument('--generator-image',help='Image built by build-stress-generator.py; enables isolated Podman workers')
    parser.add_argument('--workers',type=int,choices=range(1,13),default=1)
    parser.add_argument('--generator-memory-mib',type=int,choices=(256,512,1024,2048,4096),default=512)
    parser.add_argument('--min-rate',type=int,default=10,help='Minimum offered requests/s; recovery is always retained')
    parser.add_argument('--max-rate',type=int,choices=(10,50,100,150,200,250,500,750,1000,10000),default=10000)
    parser.add_argument('--min-free-disk-percent',type=int,default=30)
    parser.add_argument('--gateway-memory-mib',type=int,choices=(512,1024,2048,4096,8192,16384,32768),default=512,help='Local experiment only; restores the previous container limit afterward')
    parser.add_argument('--provider-memory-mib',type=int,choices=(512,1024,2048,4096),default=512,help='Isolated mock backend limit; restores its original launch afterward')
    args=parser.parse_args();os.umask(0o077)
    rows=select_phases(plan(duration=args.duration,concurrency=args.concurrency,profile=args.profile),
                       minimum=args.min_rate,maximum=args.max_rate)
    soft,_=resource.getrlimit(resource.RLIMIT_NOFILE)
    if args.generator_image is None and soft<args.concurrency+256:parser.error('Generator needs at least '+str(args.concurrency+256)+' file descriptors; current soft limit is '+str(soft)+'. Raise this process limit within the host hard limit, or lower concurrency.')
    if args.workers>1 and args.generator_image is None:parser.error('--workers requires --generator-image')
    if args.generator_image is not None:
        if args.generator_image!=parallel_generator.image_tag():parser.error('Build the generator from the reviewed sources first')
        for row in rows:
            parallel_generator.partition(users=row['users'],concurrency=row['concurrency'],
                offered_rps=row['offered_rps'],workers=args.workers)
    builder=base.load('build-stress-provider.py','stress_image_inputs')
    if args.provider_image!=builder.inputs()[1]:raise ValueError('Require the image built from current reviewed stress-provider sources')
    controller=Controller(args.praxis_image,min_disk_percent=args.min_free_disk_percent)
    stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_:stop.set())
    controller.generator_image=args.generator_image
    controller.generator_workers=args.workers
    controller.generator_memory_mib=args.generator_memory_mib
    controller.database_profile=args.database_profile
    controller.key_lifetime='8h'
    controller.keep_gateway_running=args.keep_gateway_running
    controller.gateway_memory_mib=args.gateway_memory_mib
    controller.provider_memory_mib=args.provider_memory_mib
    print('Private report: '+str(controller.output),flush=True)
    try:
        controller.guard()
        controller.prepare(max(p['users'] for p in rows))
        controller.summary['connection_policy']='bounded async HTTPS connection pool'
        controller.summary['keep_gateway_running']=args.keep_gateway_running
        controller.summary['scenario']='capacity-staircase'
        controller.summary['response_schedule']={'minimum_seconds':10,'maximum_seconds':120,'method':'coprime-stride permutation of all integer durations'}
        controller.summary['generator_fd_soft_limit']=65536 if args.generator_image else soft
        controller.summary['generator_workers']=args.workers
        controller.summary['generator']='owned Podman workers' if args.generator_image else 'local host'
        controller.summary['generator_memory_limit_mib_per_worker']=args.generator_memory_mib if args.generator_image else None
        controller.summary['generator_network']='shared pod source-address port pool' if args.generator_image else 'local host'
        controller.summary['gateway_memory_limit_mib']=args.gateway_memory_mib
        controller.summary['mock_provider_memory_limit_mib']=args.provider_memory_mib
        controller.summary['requested_max_rate']=max(p['offered_rps'] for p in rows)
        controller.summary['phase_duration_seconds']=args.duration
        controller.summary['planned_phases']=rows
        controller.summary['staircase_profile']=args.profile
        controller.summary['keep_gateway_running']=args.keep_gateway_running
        controller.save()
        controller.replace_provider(args.provider_image)
        reason=asyncio.run(exercise(controller,rows,stop))
        if not controller.verify():raise RuntimeError('Accounting reconciliation failed')
        controller.summary['requested_max_rate']=max(p['offered_rps'] for p in rows)
        controller.summary['phase_duration_seconds']=args.duration
        controller.summary['capacity_qualified']=not reason and not stop.is_set()
        controller.save()
    except Exception as error:
        if controller.output.exists():
            controller.summary['capacity_qualified']=False
            controller.summary['failure_type']=type(error).__name__
            if isinstance(error,base.local_probe.ProbeError):
                controller.summary['failure_probe']={'operation':error.operation,'kind':error.kind}
            try:
                records=json.loads(fixture.run('inspect',*[lab.POD+'-'+name for name in base.component_metrics.COMPONENTS]))
                lab.write(controller.output,'failure-services.json',[{'name':r['Name'],'state':{k:r['State'].get(k) for k in ('Running','OOMKilled','ExitCode','FinishedAt')},'memory_limit_bytes':r['HostConfig']['Memory']} for r in records])
            except Exception:controller.summary['failure_services_capture_failed']=True
            if isinstance(error,base.SafetyStop):controller.summary['failure_reason']=str(error)
            controller.summary['failure_phase']=getattr(controller,'current_phase',{}).get('name')
            controller.save()
        raise
    finally:
        if not controller.cleanup():raise RuntimeError('Cleanup incomplete; inspect private report')
    print('Finished; inspect measured throughput and escalation_stop in the private report.',flush=True)


if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit(type(error).__name__+': local stress run failed; inspect private report') from None
