#!/usr/bin/env python3
"""Qualify real Podman log ingestion/redaction through local mTLS into Loki."""
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time
import urllib.parse
import yaml

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('agent',HERE/'telemetry-agent.py')
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)
spec=importlib.util.spec_from_file_location('signals',HERE/'signals.py')
signals=importlib.util.module_from_spec(spec);spec.loader.exec_module(signals)
lab=signals.lab


def main():
    suffix=secrets.token_hex(6);name='enmaas-log-test-'+suffix
    writer=name+'-writer';collector=name+'-collector';volumes=[];secret_names=[];containers=[]
    service='enmaas-log-test-'+suffix
    def run(args,data=None,check=True):
        result=subprocess.run(['podman',*args],input=data,capture_output=True,text=True,timeout=60)
        if check and result.returncode:raise RuntimeError('Local log test operation failed; private service state may assist diagnosis')
        return result
    pod=json.loads(run(['pod','inspect',lab.POD]).stdout)[0]
    if pod['Labels'].get('io.enmaas.test-lab')!=lab.INSTANCE:raise ValueError('Unowned fixture')
    image=json.loads(run(['inspect',lab.POD+'-provider']).stdout)[0]['Image']
    started=time.time_ns()
    try:
        for kind in ('logs','positions'):
            volume=name+'-'+kind;run(['volume','create','--label',lab.LABEL,volume]);volumes.append(volume)
        mount=json.loads(run(['volume','inspect',volumes[0]]).stdout)[0]['Mountpoint']
        # A real container's stdout is written by Podman, not a hand-made CRI file.
        program="import pathlib,time\np=pathlib.Path('/control/trigger');old=''\nprint('warmup',flush=True)\nwhile True:\n value=p.read_text() if p.exists() else ''\n if value!=old:\n  print(value,flush=True);old=value\n time.sleep(.1)"
        run(['run','-d','--name',writer,'--label',lab.LABEL,'--network','none','--memory','64m','--cap-drop','all',
             '--log-driver','k8s-file','--log-opt','path='+mount+'/pricetag-gateway.log','--log-opt','max-size=10mb',
             '-v',volumes[0]+':/control',image,'python3','-u','-c',program]);containers.append(writer)
        config=agent.configuration('10.0.0.20')
        config['service']['telemetry']={'metrics':{'level':'none'}} # fixture already owns 8888
        receiver=config['receivers']['filelog/pricetag-gateway'];receiver['resource']['service.name']=service
        config['receivers']={'filelog/pricetag-gateway':receiver}
        config['service']['pipelines']={'logs':config['service']['pipelines']['logs']}
        config['service']['pipelines']['logs']['receivers']=['filelog/pricetag-gateway']
        config['exporters']['otlphttp/remote']['endpoint']='https://localhost:24443'
        contents={'config':yaml.safe_dump(config),'ca':(lab.STATE/'ca.pem').read_text(),
                  'cert':(lab.STATE/'client.pem').read_text(),'key':(lab.STATE/'client.key').read_text()}
        targets={'config':'collector.yaml','ca':'server-ca.pem','cert':'client.pem','key':'client.key'};mounts=[]
        for kind,content in contents.items():
            secret=name+'-'+kind;run(['secret','create','--label',lab.LABEL,secret,'-'],content);secret_names.append(secret)
            mounts+=['--secret',secret+',target=/lab/'+targets[kind]]
        command=['run','-d','--name',collector,'--label',lab.LABEL,'--pod',lab.POD,'--user','0:0',
                 '--cap-drop','all','--security-opt','no-new-privileges','--read-only','--memory','256m',
                 '-v',volumes[0]+':/logs:ro','-v',volumes[1]+':/positions',*mounts,lab.IMAGES['collector'],'--config=/lab/collector.yaml']
        run(command);containers.append(collector)
        def stored():
            url='http://localhost:3100/loki/api/v1/query_range?'+urllib.parse.urlencode({
                'query':'{service_name="'+service+'"}','start':started,'end':time.time_ns(),'limit':100})
            data=signals.query(url)
            return [value for row in (data or {}).get('data',{}).get('result',[]) for value in row['values']]
        for phase in range(3):
            if phase==1:run(['restart',collector])
            if phase==2:
                run(['exec',writer,'python3','-c',"from pathlib import Path;Path('/control/pricetag-gateway.log').write_text('')"])
            time.sleep(3)
            sentinel='ENMAAS_SECRET_LOG_SENTINEL_'+str(phase)
            run(['exec',writer,'python3','-c',"from pathlib import Path;Path('/control/trigger').write_text('"+sentinel+"')"])
            for _ in range(30):
                values=stored()
                if len(values)>=phase+1:break
                time.sleep(1)
            else:raise RuntimeError('Container log not delivered')
            if any(value[1]!='gateway event' for value in values):raise RuntimeError('Original log body survived')
            if len(values)!=phase+1:raise RuntimeError('Unexpected duplicate log delivery across restart')
        lab.write(lab.STATE,'log-runtime-evidence.json',{'service':service,'records':3,'redacted':True,'restart_offsets':True,'truncation_resume':True})
        print('PASS: real Podman logs reach Loki without original text; restart and truncation preserve three events')
    finally:
        for container in reversed(containers):run(['rm','-f',container])
        for secret in secret_names:run(['secret','rm',secret])
        for volume in reversed(volumes):run(['volume','rm',volume])

if __name__=='__main__':main()
