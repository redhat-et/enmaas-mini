#!/usr/bin/env python3
"""Probe generated issuer route boundaries in a disposable pod with no host ports."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('issuer_host',HERE/'keycloak-host.py')
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
spec=importlib.util.spec_from_file_location('issuer_certificates',HERE/'certificates.py')
certs=importlib.util.module_from_spec(spec);spec.loader.exec_module(certs)
PYTHON='docker.io/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
CLIENT='''import http.client,json,ssl,time,urllib.parse
ctx=ssl.create_default_context(cafile='/run/secrets/ca.pem')
def request(path,headers=None):
 c=http.client.HTTPSConnection('127.0.0.1',8443,context=ctx,timeout=5)
 try:
  c.request('GET',path,headers=headers or {});r=c.getresponse();body=r.read(65536);return r.status,body
 finally:c.close()
allowed='/realms/enmaas-lab/.well-known/openid-configuration'
last_status=None;last_error=None
for _ in range(60):
 try:
  code,body=request(allowed);last_status=code
  if code==200:break
 except OSError as error:last_error=type(error).__name__
 time.sleep(1)
else:
 print(json.dumps({'stage':'discovery','last_status':last_status,'last_error':last_error}),flush=True)
 raise SystemExit(1)
def validate_discovery(body):
 document=json.loads(body)
 assert document['issuer']=='https://127.0.0.1:8443/realms/enmaas-lab'
 for key in ('authorization_endpoint','token_endpoint','jwks_uri','userinfo_endpoint','end_session_endpoint'):
  url=urllib.parse.urlsplit(document[key])
  assert url.scheme=='https' and url.netloc=='127.0.0.1:8443'
  assert url.path.startswith('/realms/enmaas-lab/')
validate_discovery(body)
header_probes=[{'Host':'attacker.example'},
 {'X-Forwarded-Host':'attacker.example','X-Forwarded-Proto':'http','X-Forwarded-Port':'80'},
 {'Forwarded':'for=192.0.2.8;host=attacker.example;proto=http'}]
for headers in header_probes:
 code,body=request(allowed,headers)
 assert code==200
 validate_discovery(body)
paths=['/admin/','/admin/realms','/realms/master/.well-known/openid-configuration',
 '/realms/enmaas-lab/../../admin/','/resources/../admin/',
 '/realms/enmaas-lab/../master/.well-known/openid-configuration',
 '/realms/enmaas-lab/%2e%2e/master/.well-known/openid-configuration',
 '/realms/enmaas-lab/%252e%252e/master/.well-known/openid-configuration',
 '/resources/%2e%2e/admin/','/resources/..%2fadmin/']
results=[]
for path in paths:
 code,body=request(path);results.append({'path':path,'status':code,'proxy_denied':body==b'Route not available'})
print(json.dumps({'allowed_discovery':200,'forwarded_header_identity_checks':len(header_probes),'probes':results}),flush=True)
if any(row['status'] not in (400,404) for row in results):raise SystemExit(1)
assert request(allowed)[0]==200
'''


def cleanup(call,name,pod,created):
    operations=([('pod','rm','--force','--ignore',name)] if pod else [])
    operations += [('secret','rm','--ignore',secret) for secret in created]
    errors=[]
    for operation in operations:
        try:
            if call(*operation,check=False,timeout=10).returncode:errors.append(operation[-1])
        except (OSError,subprocess.TimeoutExpired):errors.append(operation[-1])
    if errors:raise RuntimeError('Issuer route cleanup failed; inspect '+','.join(errors))


def main(praxis_image=host.PRAXIS):
    os.umask(0o077)
    name='enmaas-issuer-routes-'+secrets.token_hex(6)
    created=[];pod=False
    def call(*args,input=None,check=True,timeout=180):
        result=subprocess.run(['podman',*args],input=input,text=True,capture_output=True,timeout=timeout)
        if check and result.returncode:raise RuntimeError('Issuer route test operation failed; private output suppressed')
        return result
    try:
        with tempfile.TemporaryDirectory(prefix=name+'-') as temporary:
            root=Path(temporary);(root/'issuer').mkdir()
            host.configure(root/'issuer','192.0.2.10','192.0.2.11','192.0.2.12')
            certs.generate(root/'tls',{'gateway':['192.0.2.11'],'keycloak':['127.0.0.1'],
                'observability':['192.0.2.12']},1)
            config=json.loads((root/'issuer/proxy.json').read_text())
            config['filter_chains'][0]['filters'][-1]['clusters'][0]['endpoints']=['127.0.0.1:8080']
            material={'config':json.dumps(config),'realm':(root/'issuer/realm.json').read_text(),
                'cert':(root/'tls/keycloak/server.pem').read_text(),
                'key':(root/'tls/keycloak/server.key').read_text(),
                'ca':(root/'tls/trust/issuer-ca.pem').read_text()}
            for key,value in material.items():
                secret=name+'-'+key;created.append(secret)
                call('secret','create','--label','io.enmaas.test-lab='+name,secret,'-',input=value)
            pod=True;call('pod','create','--name',name,'--label','io.enmaas.test-lab='+name)
            def run(role,image,mounts,args):
                cmd=['run','--detach','--pod',name,'--name',name+'-'+role,
                    '--label','io.enmaas.test-lab='+name,'--cap-drop','ALL',
                    '--security-opt','no-new-privileges','--memory','2g','--pids-limit','256']
                for key,target in mounts:cmd+=['--secret',name+'-'+key+',target='+target+',mode=0444']
                call(*cmd,image,*args)
            run('issuer',host.lab.IMAGES['keycloak'],[('realm','/opt/keycloak/data/import/realm.json')],
                ['start-dev','--import-realm','--http-port=8080','--hostname=https://127.0.0.1:8443'])
            run('proxy',praxis_image,[('config','/etc/praxis/config.json'),('cert','/run/secrets/lab-server-cert'),
                ('key','/run/secrets/lab-server-key')],['-c','/etc/praxis/config.json'])
            result=call('run','--name',name+'-client','--pod',name,'--cap-drop','ALL',
                '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',
                '--secret',name+'-ca,target=/run/secrets/ca.pem,mode=0444',
                '-i',PYTHON,'python3','-',input=CLIENT,check=False)
            # Client output contains only fixed test paths/status, never issuer credentials.
            if result.stdout:print(result.stdout.strip())
            if result.returncode:raise RuntimeError('Issuer route boundary probe failed')
            print('PASS: discovery available before/after forbidden-route probes')
    finally:
        cleanup(call,name,pod,created)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--praxis-image',default=host.PRAXIS,help='reviewed image override for native local tests; record separately from deployment-image qualification')
    args=parser.parse_args()
    main(args.praxis_image)
