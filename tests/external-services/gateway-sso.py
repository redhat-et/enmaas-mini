#!/usr/bin/env python3
"""Start the real rendered Praxis dashboard boundary and PriceTag in the local lab."""
import argparse
from decimal import Decimal
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import local as lab
sys.path.insert(0,str(lab.ROOT/'scripts/pricetag'))
import prepare
import oidc_config


def run(*args,input=None):
    return subprocess.run(['podman',*map(str,args)],input=input,check=True,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout


def secret(name,path):
    run('secret','create','--label',lab.LABEL,lab.secret_name(name),'-',input=path.read_bytes())
    return lab.secret_name(name)


def maas_environment():
    # One trusted fixture namespace. This is not a cross-host trust policy.
    return {'MAAS_RUNTIME_MODE':'standalone','MAAS_SUBSCRIPTION_MODE':'standalone',
        'ADDRESS':'127.0.0.1:8088','SECURE':'false',
        'DB_CONNECTION_URL_FILE':'/run/secrets/maas-db-url',
        'API_KEY_MAX_EXPIRATION_DAYS':'1','MAAS_ADMIN_GROUPS':'single-server-admin',
        'MAAS_TRUSTED_PROXY_CIDRS':'127.0.0.1/32',
        'METRICS_SECURE':'false','METRICS_ADDRESS':'127.0.0.1:9909'}


def run_mock_dependencies(maas, profiles=False):
    common=['--detach','--pod',lab.POD,'--label',lab.LABEL,'--read-only',
        '--cap-drop','ALL','--security-opt','no-new-privileges',
        '--pids-limit','256','--memory','512m','--tmpfs','/tmp:rw,noexec,nosuid,size=16m']
    env=[]
    for key,value in maas_environment().items():env.extend(['--env',key+'='+value])
    run('run',*common,'--name',lab.POD+'-maas',*env,
        '--secret',lab.secret_name('gateway-maas-db')+',target=/run/secrets/maas-db-url,mode=0444',maas)
    run('run',*common,'--name',lab.POD+'-provider',
        '--secret',lab.secret_name('gateway-provider')+',target=/fixtures/provider.py,mode=0444',
        '--secret',lab.secret_name('gateway-provider-start')+',target=/fixture-start.py,mode=0444',
        *(['--env', 'MOCK_PROFILES=1', '--secret', lab.secret_name('mock-profiles')+',target=/fixtures/mock-profiles.json,mode=0444'] if profiles else []),
        prepare.PYTHON,'python3','-B','/fixture-start.py')


def start(metering,praxis,maas=None,profiles=False):
    metadata=json.loads(run('pod','inspect',lab.POD))[0]
    if metadata.get('Labels',{}).get('io.enmaas.test-lab')!=lab.INSTANCE:
        raise ValueError('Local lab ownership label differs')
    reviewed=[(metering,'2281e1a84bfc3b998e7db36bc01bae88130bfcdd'),
                           (praxis,'5667c4acecd0981ae5114cbbf2ee3dcccec9c248')]
    if maas:reviewed.append((maas,'6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d'))
    for image,revision in reviewed:
        info=json.loads(run('image','inspect',image))[0]
        if info.get('Labels',{}).get('org.opencontainers.image.revision')!=revision:
            raise ValueError('Use the reviewed source revision for this test')
    if (lab.STATE/'gateway-sso.env').exists():
        raise ValueError('Existing SSO test inputs; inspect and clean the dedicated fixture before retrying')
    credentials=json.loads((lab.STATE/'credentials.json').read_text())
    m2m=secrets.token_hex(32);partner=secrets.token_hex(32)
    env,_,mapping=oidc_config.load(lab.STATE/'gateway-oidc.json','https://localhost:28443')
    if mapping is not None:raise ValueError('shared-pod fixture does not support private issuer mappings')
    env.update(DATABASE_URL=f"postgres://keycloak:{credentials['db']}@localhost:5432/metering_sso?sslmode=disable",
        PORT='8087',METRICS_PORT='9908',M2M_AUTH_REQUIRED='true',M2M_SHARED_SECRET=m2m,
        MANAGEMENT_API_SECRET=secrets.token_hex(32),MANAGEMENT_PUBLIC_ORIGIN='https://localhost:28443',PARTNER_USER_KEY_GROUP='system:authenticated',
        USER_MANAGEMENT_API_SECRET=partner,SESSION_SECRET=secrets.token_hex(32),SESSION_COOKIE_SECURE='true')
    if maas:
        env.update(KEY_SERVICE_URL='http://127.0.0.1:8088',KEY_SERVICE_USER_HEADER='X-MaaS-Username',
            KEY_SERVICE_GROUPS_HEADER='X-MaaS-Group',DEFAULT_KEY_EXPIRY_HOURS='24',MAX_KEY_EXPIRY_HOURS='24',
            MAAS_VALIDATE_URL='http://127.0.0.1:8088/internal/v1/api-keys/validate',MONTHLY_TOKEN_QUOTA='10000000000')
    lab.write(lab.STATE,'gateway-sso.env',''.join(f'{key}={value}\n' for key,value in env.items()))
    lab.write(lab.STATE,'gateway-sso-partner',partner)
    lab.write(lab.STATE,'gateway-sso-m2m',m2m)
    if profiles:
        from mock_profiles import configuration
        from demo_config import native_inputs
        document=configuration()
        original,models=native_inputs(document),document['models']
        lab.write(lab.STATE,'mock-profiles.json',document)
        secret('mock-profiles',lab.STATE/'mock-profiles.json')
        for name,key in [('openai','synthetic-openai'),('external','synthetic-anthropic')]:
            lab.write(lab.STATE,'mock-key-'+name,key)
            secret('mock-key-'+name,lab.STATE/('mock-key-'+name))
    else:
        original,models=prepare.mock_inputs()
    config=prepare.render(original,models)
    config['listeners'][0]['address']='0.0.0.0:28443'
    dashboard=next(c for c in config['filter_chains'] if c['name']=='dashboard')
    dashboard['filters'][-1]['clusters'][0]['endpoints']=['127.0.0.1:8087']
    # Only fixture network addresses/TLS file locations differ. Retain the real
    # public allowlist, rate limit, private dashboard chain and header stripping.
    config=json.loads(json.dumps(config).replace('pricetag-metering:8080','127.0.0.1:8087').replace('pricetag-maas:8080','127.0.0.1:8088').replace('pricetag-provider:','127.0.0.1:'))
    lab.write(lab.STATE,'gateway-sso.json',config)
    run('exec',lab.POD+'-db','createdb','-U','keycloak','metering_sso')
    if maas:
        run('exec',lab.POD+'-db','createdb','-U','keycloak','maas_fixture')
        lab.write(lab.STATE,'gateway-maas-db',f"postgres://keycloak:{credentials['db']}@127.0.0.1:5432/maas_fixture?sslmode=disable")
        for name,path in [('gateway-maas-db',lab.STATE/'gateway-maas-db'),
                          ('gateway-provider',lab.ROOT/'tests/common/provider.py'),
                          ('gateway-provider-start',lab.ROOT/'tests/pricetag/provider.py')]:secret(name,path)
        run_mock_dependencies(maas,profiles=profiles)
    for name in ('gateway-sso-m2m','gateway-sso.json'):secret(name,lab.STATE/name)
    common=['--detach','--pod',lab.POD,'--label',lab.LABEL,'--cap-drop','ALL',
            '--security-opt','no-new-privileges','--pids-limit','256','--memory','512m']
    run('run',*common,'--name',lab.POD+'-metering-sso','--env-file',lab.STATE/'gateway-sso.env',
        '--secret',lab.secret_name('ca.pem')+',target=/etc/pricetag-oidc-ca.pem,mode=0444',metering)
    for _ in range(60):
        # Request through a disposable client inside the same namespace; no
        # metering API or partner secret is published on the host.
        result=subprocess.run(['podman','exec',lab.POD+'-prometheus','wget','-qO-',
                               'http://127.0.0.1:8087/ready'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode==0:break
        time.sleep(1)
    else:raise ValueError('Metering readiness timeout; inspect private logs')
    start_gateway(praxis)
    lab.wait_url('https://localhost:28443/login')
    print('Real rendered gateway and OIDC-enabled PriceTag ready; identity/browser tests remain separate.')

def start_gateway(praxis,telemetry=False,service_name="enmaas-gateway"):
    run('run','--detach','--pod',lab.POD,'--label',lab.LABEL,'--cap-drop','ALL',
        '--security-opt','no-new-privileges','--pids-limit','256','--memory','512m','--name',lab.POD+'-gateway-sso',
        '--secret',lab.secret_name('gateway-sso.json')+',target=/etc/praxis/config.json,mode=0444',
        '--secret',lab.secret_name('gateway-sso-m2m')+',target=/run/secrets/pricetag-m2m,mode=0444',
        '--secret',lab.secret_name('server.pem')+',target=/etc/praxis/tls.pem,mode=0444',
        '--secret',lab.secret_name('server.key')+',target=/etc/praxis/tls-key.pem,mode=0444',
        *(['-e','OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317','-e','OTEL_SERVICE_NAME='+service_name] if telemetry else []),
        *(['--secret',lab.secret_name('mock-key-openai')+',type=env,target=OPENAI_API_KEY',
           '--secret',lab.secret_name('mock-key-external')+',type=env,target=CUSTOM_EXTERNAL_API_KEY']
          if (lab.STATE/'mock-profiles.json').exists() else []),
        praxis,'--config','/etc/praxis/config.json')

def test():
    env=dict(os.environ)
    env['NODE_EXTRA_CA_CERTS']=str(lab.STATE/'ca.pem')
    env.setdefault('NODE_PATH',str(lab.ROOT/'tests/pricetag/browser/node_modules'))
    def browser(mode):
        subprocess.run(['node',str(HERE/'gateway-sso.cjs'),str(lab.STATE),mode],check=True,env=env)
    browser('unknown')
    for name,path in [('gateway-sso-partner',lab.STATE/'gateway-sso-partner'),
                      ('gateway-sso-identities',lab.STATE/'identities.json'),
                      ('gateway-sso-provision',HERE/'gateway-identities.py')]:
        secret(name,path)
    partner_action()
    browser('lifecycle')
    lab.write(lab.STATE,'gateway-sso-evidence.json',{'passed':True,
        'checks':['unknown_identity_denied','anonymous_partner_write_denied','immutable_identity_login',
                  'issuer_group_not_admin','private_partner_route_denied','active_session_deactivation',
                  'inactive_identity_relogin_denied','shared_sso_observability_group_gate'],
        'not_qualified':['shared_logout','role_sync','inference_accounting','RHEL']})


def reconcile_probe(subject):
    if not re.fullmatch(r'lab-probe-[a-f0-9]{32}',subject):
        raise ValueError('Invalid isolated probe subject')
    query=("SELECT count(*),coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0),"
           "coalesce(sum(total_tokens),0),count(cost_usd),coalesce(sum(cost_usd),0) FROM usage_events WHERE username='"+subject+"';")
    for _ in range(30):
        row=run('exec',lab.POD+'-db','psql','-U','keycloak','-d','metering_sso',
                '-At','-v','ON_ERROR_STOP=1','-c',query).decode().strip()
        fields=row.split('|')
        if len(fields)!=6:raise ValueError('Unexpected ledger aggregate')
        values=tuple(int(value) for value in fields[:5])
        cost=Decimal(fields[5])
        if values==(2,4,6,10,2) and cost==Decimal('0.000016'):return
        if values[0]>2:raise ValueError('Duplicate ledger events')
        time.sleep(1)
    raise ValueError('Mock usage ledger does not reconcile')


def inference_probe():
    values=dict(line.split('=',1) for line in (lab.STATE/'gateway-sso.env').read_text().splitlines())
    if values.get('KEY_SERVICE_URL')!='http://127.0.0.1:8088':
        raise ValueError('Start the fresh fixture with --maas-image first')
    lab.write(lab.STATE,'gateway-management',values['MANAGEMENT_API_SECRET'])
    mounts=[]
    for name,path,target in [('gateway-management',lab.STATE/'gateway-management','management'),
                             ('inference-probe',HERE/'inference-probe.py','probe.py'),
                             ('inference-transport',HERE/'transport.py','transport.py')]:
        secret(name,path)
        mounts.extend(['--secret',lab.secret_name(name)+',target=/fixture/'+target+',mode=0444'])
    output=run('run','--rm','--pod',lab.POD,'--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',*mounts,
        '--secret',lab.secret_name('ca.pem')+',target=/fixture/ca.pem,mode=0444',
        prepare.PYTHON,'python3','-B','/fixture/probe.py')
    result=json.loads(output)
    reconcile_probe(result['subject'])
    result['ledger_reconciled']=True
    result['recorded_usd']='0.000016'
    lab.write(lab.STATE,'inference-probe-evidence.json',result)
    print('PASS: authenticated JSON/SSE mock inference; probe key revoked and account disabled. Two independent ledger events match expected token totals.')


def role_action(role):
    if role not in ('user','super_admin'):raise ValueError('Unsupported test role')
    values=dict(line.split('=',1) for line in (lab.STATE/'gateway-sso.env').read_text().splitlines())
    lab.write(lab.STATE,'gateway-management',values['MANAGEMENT_API_SECRET'])
    names=[];mounts=[]
    try:
        for suffix,path,target in [('auth',lab.STATE/'gateway-management','management'),('client',HERE/'management-client.py','client.py')]:
            name='role-test-'+secrets.token_hex(6)+'-'+suffix
            secret(name,path);names.append(name)
            mounts.extend(['--secret',lab.secret_name(name)+',target=/fixture/'+target+',mode=0444'])
        run('run','--rm','--interactive','--pod',lab.POD,'--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',*mounts,
            prepare.PYTHON,'python3','-B','/fixture/client.py',input=json.dumps({
                'route':'users','method':'PUT','body':{'subject':'admin@example.test','role':role}}).encode())
    finally:
        for name in names:run('secret','rm',lab.secret_name(name))
    print('Synthetic gateway test role updated through management API')


def partner_action(action='provision'):
    output=run('run','--rm','--pod',lab.POD,'--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--memory','128m','--pids-limit','64',
        '--secret',lab.secret_name('gateway-sso-partner')+',target=/fixture/partner,mode=0444',
        '--secret',lab.secret_name('gateway-sso-identities')+',target=/fixture/identities.json,mode=0444',
        '--secret',lab.secret_name('gateway-sso-provision')+',target=/fixture/provision.py,mode=0444',
        prepare.PYTHON,'python3','/fixture/provision.py',action)
    # The fixture script emits fixed PASS messages only; no service response bodies.
    print(output.decode().strip())


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('start','test','deactivate','inference','promote','demote'))
    parser.add_argument('--metering-image')
    parser.add_argument('--praxis-image')
    parser.add_argument('--mock-profiles',action='store_true',help='Use standard openai/external profile aliases with mock endpoints')
    parser.add_argument('--maas-image',help='Enable private MaaS and deterministic mock providers')
    args=parser.parse_args()
    if args.action=='start' and not (args.metering_image and args.praxis_image):
        parser.error('start requires both reviewed application images')
    if args.mock_profiles and (args.action!='start' or not args.maas_image):
        parser.error('--mock-profiles requires start with --maas-image')
    try:
        if args.action=='start':start(args.metering_image,args.praxis_image,args.maas_image,profiles=args.mock_profiles)
        elif args.action in ('promote','demote'):role_action('super_admin' if args.action=='promote' else 'user')
        elif args.action=='inference':inference_probe()
        elif args.action=='deactivate':partner_action('deactivate')
        else:test()
    except (ValueError,subprocess.SubprocessError,OSError):
        raise SystemExit('Gateway SSO fixture failed; inspect private fixture state. No automatic deletion.') from None
