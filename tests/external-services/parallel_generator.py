"""Run bounded nonroot generator workers inside the selected owned mock pod."""
import asyncio
import importlib.util
import json
from pathlib import Path
import uuid

import local as lab
from parallel_results import COUNTERS, partition, merge
from collections import Counter

HERE=Path(__file__).resolve().parent
MAX_LINE=1024*1024


def image_tag():
    spec=importlib.util.spec_from_file_location('parallel_generator_image',HERE/'build-stress-generator.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    return builder.inputs()[1]


def command(name, image, memory_mib):
    if image!=image_tag():raise ValueError('Generator image must match the reviewed sources')
    if type(memory_mib) is not int or memory_mib not in (256,512,1024,2048,4096):
        raise ValueError('Invalid worker memory limit')
    if not name.startswith(lab.POD+'-generator-') or not name.removeprefix(lab.POD+'-generator-').isalnum():
        raise ValueError('Invalid owned worker name')
    return ['podman','create','--interactive','--pull=never','--name',name,
        '--label',lab.LABEL,'--pod',lab.POD,'--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--memory',str(memory_mib)+'m','--pids-limit','64',
        '--ulimit','nofile=65536:65536',
        '--secret',lab.secret_name('ca.pem')+',target=/fixture/ca.pem,mode=0444',image]


async def podman(*args):
    process=await asyncio.create_subprocess_exec('podman',*args,stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        output,_=await asyncio.wait_for(process.communicate(),timeout=15)
        return process.returncode,output
    finally:
        if process.returncode is None:
            process.kill();await process.wait()


async def create_owned(argv):
    # Finish the bounded create operation before cancellation can trigger cleanup.
    # The worker has not started yet, so no inference can escape this boundary.
    task=asyncio.create_task(podman(*argv[1:]))
    try:
        code,_=await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    if code:raise RuntimeError('Generator container creation failed')


async def cleanup(name):
    code,raw=await podman('container','exists',name)
    if code==1:return
    if code:raise RuntimeError('Cannot establish worker ownership for cleanup')
    code,raw=await podman('inspect',name)
    if code:raise RuntimeError('Worker inspection failed')
    record=json.loads(raw)[0]
    if record.get('Config',{}).get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE:
        raise RuntimeError('Refusing to remove an unowned generator')
    evidence={'name':name,'state':{key:record['State'].get(key) for key in ('Running','OOMKilled','ExitCode')},
              'memory_limit_bytes':record['HostConfig']['Memory']}
    code,_=await podman('rm','--force',name)
    if code:raise RuntimeError('Owned generator cleanup failed')
    return evidence


async def single(request, allocation, *, image, memory_mib, duration, timeout, stop, sample, evidence):
    name=lab.POD+'-generator-'+uuid.uuid4().hex[:12]
    start,end=allocation['start'],allocation['end']
    definition=request.definition
    payload={'port':lab.port('gateway'),'keys':definition.keys[start:end],
        'model':json.loads(definition.body)['model'],'profiles':definition.profiles[start:end],
        'user_agents':definition.user_agents[start:end],
        'phase':{'concurrency':allocation['concurrency'],'duration':duration,
                 'offered_rps':allocation['offered_rps'],'minimum_seconds':request.minimum,
                 'maximum_seconds':request.maximum,'timeout':timeout}}
    raw=json.dumps(payload,allow_nan=False).encode()+b'\n'
    if len(raw)>4*1024*1024:raise ValueError('Worker credential input exceeds bound')
    process=None
    try:
        async with asyncio.timeout(duration+timeout+60):
            await create_owned(command(name,image,memory_mib))
            process=await asyncio.create_subprocess_exec('podman','start','--attach','--interactive',name,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,limit=MAX_LINE)
            process.stdin.write(raw);await process.stdin.drain();process.stdin.close()
            result=None
            while True:
                if stop.is_set():raise InterruptedError('Generator cancelled')
                line=await asyncio.wait_for(process.stdout.readline(),timeout=30)
                if not line:break
                reply=json.loads(line)
                if not isinstance(reply,dict) or set(reply)!={'kind','data'}:
                    raise RuntimeError('Invalid generator protocol')
                if reply['kind']=='sample' and result is None:sample(reply['data'])
                elif reply['kind']=='result' and result is None:result=reply['data']
                else:raise RuntimeError('Generator failed or returned duplicate results')
            code=await asyncio.wait_for(process.wait(),timeout=10)
            if code or result is None:raise RuntimeError('Generator exited without valid results')
            return result
    finally:
        try:
            record=await cleanup(name)
            if record is not None:evidence.append(record)
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:await asyncio.wait_for(process.wait(),timeout=5)
                except TimeoutError:process.kill();await process.wait()


async def run_phase(request, *, users, concurrency, duration, offered_rps, timeout,
                    stop, sample, workers, image, memory_mib=512, evidence=None):
    allocations=partition(users=users,concurrency=concurrency,offered_rps=offered_rps,workers=workers)
    if len(request.definition.keys)!=users:raise ValueError('Worker identities differ from the phase population')
    # Revalidate ownership before any worker joins a network namespace.
    code,raw=await podman('pod','inspect',lab.POD)
    if code or json.loads(raw)[0].get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE:
        raise RuntimeError('An owned local mock pod is required')
    snapshots=[None]*workers;peak=0;tasks=[]
    evidence=[] if evidence is None else evidence
    def observe(index, value):
        nonlocal peak
        if not isinstance(value,dict):raise ValueError('Invalid worker snapshot')
        snapshots[index]=value
        available=[row for row in snapshots if row is not None]
        merged={key:sum(row[key] for row in available) for key in (*COUNTERS,'active_requests','completed')}
        for key in ('statuses','errors','os_errors'):
            counts=Counter()
            for row in available:counts.update(row[key])
            merged[key]=dict(counts)
        peak=max(peak,merged['active_requests'])
        begin=min(row['started_monotonic'] for row in available)
        merged.update(peak_in_flight=peak,started_monotonic=begin,
            elapsed_seconds=max(row['started_monotonic']+row['elapsed_seconds'] for row in available)-begin)
        sample(merged)
    try:
        for index,allocation in enumerate(allocations):
            tasks.append(asyncio.create_task(single(request,allocation,image=image,memory_mib=memory_mib,
                duration=duration,timeout=timeout,stop=stop,sample=lambda value,i=index:observe(i,value),evidence=evidence)))
        rows=await asyncio.gather(*tasks)
        result=merge(rows,sampled_peak=peak)
        result['generator_location']='Nonroot workers in the owned Podman pod; shared source-address port pool'
        return result
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
