#!/usr/bin/env python3
"""Fresh persona/API qualification in isolated databases within the local test pod.

No host port is added. Existing services, users and benchmark data are unchanged.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
FILES=('tests/external-services/persona-runtime.py','tests/external-services/personas.py',
       'tests/external-services/import-population.py','tests/external-services/user-population.py',
       'tests/external-services/keycloak-password.py','scripts/pricetag/oidc-user',
       'scripts/pricetag/metering_admin.py','scripts/pricetag/api')


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


def probe():
    import urllib.request
    personas=load('runtime_personas',HERE/'personas.py')
    original=urllib.request.Request
    partner=Path('/fixture/partner').read_text().strip()
    management_secret=Path('/fixture/management').read_text().strip()
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}),personas.population.oidc.NoRedirect())
    def management(method,resource,body=None):
        req=original('http://127.0.0.1:8089/api/v1/management/'+resource,method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+management_secret,'Content-Type':'application/json'})
        with client.open(req,timeout=30) as response:return json.load(response)
    def provision(profile):
        req=original('http://127.0.0.1:8089/api/v1/users/'+profile['user_id'],method='PUT',
            data=json.dumps({'tags':profile['tags']}).encode(),
            headers={'Authorization':'Bearer '+partner,'Content-Type':'application/json'})
        with client.open(req,timeout=30) as response:return json.load(response)
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);bootstrap='bootstrap-'+secrets.token_hex(8)
        management('PUT','users',{'subject':bootstrap,'role':'super_admin','active':True})
        key=management('POST','keys',{'subject':bootstrap,'name':'persona-runtime','expiresIn':'1h'})
        key_path=root/'bootstrap.key';key_path.touch(mode=0o600);key_path.write_text(key['key']+'\n');key.clear()
        calls=[]
        def public(method,path,body):
            index=str(len(calls));calls.append(path)
            personas.state.save(root,index+'-input.json',body,exclusive=True)
            command=[sys.executable,str(ROOT/'scripts/pricetag/api'),method,path,
                '--url','https://127.0.0.1:28444','--origin','https://localhost:28444',
                '--key-file',str(key_path),'--ca-file','/fixture/ca.pem',
                '--input',str(root/(index+'-input.json')),'--output',str(root/(index+'-output.json'))]
            result=subprocess.run(command,capture_output=True,timeout=90)
            if result.returncode:raise ValueError('Public API setup failed')
            return personas.state.load(root,index+'-output.json')
        identities={name:str(uuid.uuid4()) for name in personas.NAMES}
        state=root/'receipts'
        result=personas.apply(identities,state,provision,management,public)
        if result.get('dashboard_personas_configured') is not True or len(calls)!=3:
            raise ValueError('Persona setup incomplete')
        if personas.apply(identities,state,provision,management,public)!=result or len(calls)!=3:
            raise ValueError('Completed persona writes were replayed')
        # Refuse changed role on resume instead of silently elevating it again.
        management('PUT','users',{'subject':'alice@example.test','role':'user'})
        try:personas.apply(identities,state,provision,management,public)
        except ValueError:pass
        else:raise ValueError('Role drift was silently repaired')
        if len(calls)!=3:raise ValueError('Drift triggered a privilege write')
    print('PASS: fresh persona UUID import; authenticated roles and team assignment; USD budgets; no-write resume; role drift refused')


def main():
    sys.path.insert(0,str(HERE))
    sso=load('persona_runtime_sso',HERE/'gateway-sso.py')
    metadata=json.loads(sso.run('pod','inspect',sso.lab.POD))[0]
    if metadata.get('Labels',{}).get('io.enmaas.test-lab')!=sso.lab.INSTANCE:
        raise ValueError('Foreign local pod')
    suffix=secrets.token_hex(6);prefix=sso.lab.POD+'-persona-'+suffix
    databases=['persona_metering_'+suffix,'persona_maas_'+suffix]
    containers=[];created_databases=[];secret_names=[]
    def secret(kind,data):
        name=prefix+'-'+kind
        sso.run('secret','create','--label',sso.lab.LABEL,name,'-',input=data)
        secret_names.append(name);return name
    def image(component):
        info=json.loads(sso.run('inspect',sso.lab.POD+'-'+component))[0]
        if info.get('Config',{}).get('Labels',{}).get('io.enmaas.test-lab')!=sso.lab.INSTANCE:
            raise ValueError('Foreign source container')
        return info['Image']
    images={name:image(component) for name,component in [('maas','maas'),('metering','metering-sso'),('gateway','gateway-sso')]}
    common=['--detach','--pod',sso.lab.POD,'--label',sso.lab.LABEL,'--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--pids-limit','256','--memory','512m','--tmpfs','/tmp:rw,noexec,nosuid,size=32m']
    def launch(kind,*args):
        name=prefix+'-'+kind;containers.append(name)
        sso.run('run',*common,'--name',name,*args)
    try:
        credentials=json.loads((sso.lab.STATE/'credentials.json').read_text())
        for name in databases:
            sso.run('exec',sso.lab.POD+'-db','createdb','-U','keycloak',name);created_databases.append(name)
        dsn=lambda name:f"postgres://keycloak:{credentials['db']}@127.0.0.1:5432/{name}?sslmode=disable"
        db_secret=secret('maas-db',dsn(databases[1]).encode())
        env=sso.maas_environment();env.update(ADDRESS='127.0.0.1:8090',METRICS_ADDRESS='127.0.0.1:9911')
        args=[]
        for name,value in env.items():args.extend(['--env',name+'='+value])
        launch('maas',*args,'--secret',db_secret+',target=/run/secrets/maas-db-url,mode=0444',images['maas'])
        env=dict(line.split('=',1) for line in (sso.lab.STATE/'gateway-sso.env').read_text().splitlines())
        env.update(DATABASE_URL=dsn(databases[0]),PORT='8089',METRICS_PORT='9910',
            M2M_SHARED_SECRET=secrets.token_hex(32),MANAGEMENT_API_SECRET=secrets.token_hex(32),
            USER_MANAGEMENT_API_SECRET=secrets.token_hex(32),SESSION_SECRET=secrets.token_hex(32),
            MANAGEMENT_PUBLIC_ORIGIN='https://localhost:28444',KEY_SERVICE_URL='http://127.0.0.1:8090',
            MAAS_VALIDATE_URL='http://127.0.0.1:8090/internal/v1/api-keys/validate')
        management=secret('management',env['MANAGEMENT_API_SECRET'].encode())
        partner=secret('partner',env['USER_MANAGEMENT_API_SECRET'].encode())
        m2m=secret('m2m',env['M2M_SHARED_SECRET'].encode())
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'metering.env';path.touch(mode=0o600)
            path.write_text(''.join(name+'='+value+'\n' for name,value in env.items()))
            launch('metering','--env-file',path,'--secret',sso.lab.secret_name('ca.pem')+',target=/etc/pricetag-oidc-ca.pem,mode=0444',images['metering'])
        for _ in range(60):
            try:sso.run('exec',sso.lab.POD+'-prometheus','wget','-qO-','http://127.0.0.1:8089/ready');break
            except subprocess.CalledProcessError:time.sleep(1)
        else:raise ValueError('Fresh metering readiness failed')
        config=(sso.lab.STATE/'gateway-sso.json').read_text()
        for old,new in [('28443','28444'),('18082','18182'),('18083','18183'),('18085','18185'),('8087','8089'),('8088','8090')]:
            config=config.replace(':'+old,':'+new)
        config_secret=secret('gateway-config',config.encode())
        launch('gateway','--secret',config_secret+',target=/etc/praxis/config.json,mode=0444',
            '--secret',m2m+',target=/run/secrets/pricetag-m2m,mode=0444',
            '--secret',sso.lab.secret_name('server.pem')+',target=/etc/praxis/tls.pem,mode=0444',
            '--secret',sso.lab.secret_name('server.key')+',target=/etc/praxis/tls-key.pem,mode=0444',
            *(['--secret',sso.lab.secret_name('mock-key-openai')+',type=env,target=OPENAI_API_KEY',
               '--secret',sso.lab.secret_name('mock-key-external')+',type=env,target=CUSTOM_EXTERNAL_API_KEY']
              if (sso.lab.STATE/'mock-profiles.json').exists() else []),
            images['gateway'],'--config','/etc/praxis/config.json')
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode='w') as archive:
            for name in FILES:archive.add(ROOT/name,arcname=name,recursive=False)
        bootstrap="import tarfile,sys,runpy; tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/tmp/probe',filter='data'); sys.argv=['persona-runtime','--probe']; runpy.run_path('/tmp/probe/tests/external-services/persona-runtime.py',run_name='__main__')"
        name=prefix+'-client';containers.append(name)
        output=sso.run('run','--rm','--name',name,'--interactive','--pull=never','--pod',sso.lab.POD,
            '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=16m',
            '--secret',management+',target=/fixture/management,mode=0400',
            '--secret',partner+',target=/fixture/partner,mode=0400',
            '--secret',sso.lab.secret_name('ca.pem')+',target=/fixture/ca.pem,mode=0444',
            sso.prepare.PYTHON,'python3','-B','-c',bootstrap,input=payload.getvalue())
        print(output.decode().strip())
    finally:
        failures=[]
        cleanup=[('rm','--force','--ignore',name) for name in reversed(containers)]
        cleanup += [('secret','rm',name) for name in reversed(secret_names)]
        cleanup += [('exec',sso.lab.POD+'-db','dropdb','--force','-U','keycloak',name) for name in reversed(created_databases)]
        for command in cleanup:
            try:sso.run(*command)
            except Exception:failures.append(command[0])
        if failures:raise ValueError('Temporary fixture cleanup incomplete; inspect labelled resources')


if __name__=='__main__':
    try:
        if sys.argv[1:]==['--probe']:probe()
        elif sys.argv[1:]:raise ValueError('Unexpected runtime arguments')
        else:main()
    except Exception:
        raise SystemExit('Fresh persona runtime failed; inspect private fixture diagnostics. No credentials printed.') from None
