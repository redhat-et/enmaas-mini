#!/usr/bin/env python3
"""Refresh monitoring in an owned, loopback-only local fixture; preserve data and auth."""
import importlib.util
from contextlib import contextmanager
import fcntl
import os
import json
from pathlib import Path
import subprocess
import uuid
import yaml

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('lab',HERE/'local.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)


def replacement(command,image,mounts):
    """Replace only requested secret targets, preserving every other launch option."""
    result=[];index=0
    while index<len(command):
        if command[index]=='--secret' and any('target='+target+',' in command[index+1] for target in mounts):
            index+=2;continue
        result.append(command[index]);index+=1
    position=result.index(image)
    args=[]
    for target,secret in mounts.items():args+=['--secret',secret+',target='+target+',mode=0444']
    return result[:position]+args+result[position:]


@contextmanager
def monitoring_lock(path):
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Monitoring update refused during an active benchmark') from None
        yield
    finally:os.close(descriptor)


def main():
    with monitoring_lock(lab.STATE/'.benchmark.lock'):
        refresh()


def canonical(value):
    if isinstance(value,list):return [canonical(item) for item in value]
    if isinstance(value,dict):return {key:canonical(item) for key,item in value.items()
        if not (key=='targetBlank' and item is False or key=='description' and item=='')}
    return value


def check_dashboards(query):
    """A healthy proxy is insufficient: provisioned dashboard JSON must be readable."""
    for expected in lab.monitoring.all_dashboards().values():
        name=expected['metadata']['name']
        reply=query('http://127.0.0.1:8080/api/v1/projects/praxis/dashboards/'+name)
        if not isinstance(reply,dict) or reply.get('metadata',{}).get('name')!=name or not reply.get('spec',{}).get('panels'):
            raise RuntimeError('Perses dashboard API unavailable: '+name)
        if canonical(reply['spec']['panels'])!=canonical(expected['spec']['panels']):
            raise RuntimeError('Perses dashboard differs from source: '+name)


def refresh():
    lab.check_port_bindings()
    root=lab.STATE
    documents={}
    for component in ('prometheus','perses','collector'):
        record=json.loads(subprocess.check_output(['podman','inspect',lab.POD+'-'+component]))[0]
        if record['Config'].get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE:
            raise ValueError('Unowned component')
        documents[component]=record
    prom=yaml.safe_load((root/'prometheus.yaml').read_text())
    prom['rule_files']=['/lab/monitoring-rules.yaml'];prom.setdefault('global',{})['evaluation_interval']='15s'
    collector=yaml.safe_load((root/'collector.yaml').read_text())
    collector['processors']['transform']=lab.privacy_transform()
    collector['processors'].pop('transform/privacy',None)
    collector['exporters']['prometheus']['send_timestamps']=True
    updates={
      'prometheus':{'/lab/prometheus.yaml':yaml.safe_dump(prom),'/lab/monitoring-rules.yaml':yaml.safe_dump(lab.monitoring.rules(),sort_keys=False)},
      'perses':{'/etc/perses/lab/'+name:json.dumps(document,indent=2)+'\n' for name,document in lab.monitoring.all_dashboards().items()},
      'collector':{'/lab/collector.yaml':yaml.safe_dump(collector)},
    }
    token=uuid.uuid4().hex[:8]
    for component,files in updates.items():
        if all((root/Path(target).name).exists() and (root/Path(target).name).read_text()==content for target,content in files.items()):
            print('Unchanged '+component,flush=True);continue
        record=documents[component];mounts={}
        for index,(target,content) in enumerate(files.items()):
            name=lab.POD+'-monitor-'+token+'-'+component+'-'+str(index)
            subprocess.run(['podman','secret','create','--label',lab.LABEL,name,'-'],input=content.encode(),check=True,stdout=subprocess.DEVNULL)
            mounts[target]=name
        original=record['Config']['CreateCommand'];image=record['ImageName']
        if image not in original:image=record['Image']
        if image not in original:
            image=next(value for value in original if value==record['Image'].removeprefix('sha256:'))
        if component=='prometheus':
            subprocess.run(['podman','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
              '--secret',mounts['/lab/monitoring-rules.yaml']+',target=/lab/rules.yaml,mode=0444','--entrypoint','/bin/promtool',image,'check','rules','/lab/rules.yaml'],check=True)
        if component=='collector':
            # Reuse the existing certificate mounts to validate the complete configuration.
            check=replacement(original,image,mounts)
            check[check.index('--name')+1]+='-validate-'+token
            for flag in ('--detach','-d'):
                if flag in check:check.remove(flag)
            check.insert(2,'--rm');check[check.index(image)+1:]=['validate','--config=/lab/collector.yaml']
            subprocess.run(check,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        command=replacement(original,image,mounts)
        subprocess.run(['podman','rm','-f',record['Name']],check=True,stdout=subprocess.DEVNULL)
        try:subprocess.run(command,check=True,stdout=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            subprocess.run(['podman','rm','-f',record['Name']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(original,check=True,stdout=subprocess.DEVNULL)
            raise RuntimeError('Update failed; original launch restored') from None
        for target,content in files.items():lab.write(root,Path(target).name,content)
        print('Updated '+component+'; existing storage and authentication retained',flush=True)
    lab.check()
    import signals
    check_dashboards(signals.query)
    print("PASS: every provisioned dashboard API returns readable panels",flush=True)

if __name__=='__main__':
    try:main()
    except RuntimeError as error:raise SystemExit(str(error)) from None
