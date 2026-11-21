#!/usr/bin/env python3
"""Test population import in an existing local mock stack; never targets RHEL."""
import io
import argparse
import json
import secrets
import re
import subprocess
from pathlib import Path
import sys
import tarfile

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import importlib.util
spec=importlib.util.spec_from_file_location('sso',HERE/'gateway-sso.py')
sso=importlib.util.module_from_spec(spec);spec.loader.exec_module(sso)

FILES=(
    'tests/external-services/population-api-probe.py',
    'tests/external-services/import-population.py',
    'tests/external-services/population-key.py',
    'tests/external-services/user-population.py',
    'tests/external-services/keycloak-password.py',
    'scripts/pricetag/oidc-user',
    'scripts/pricetag/api',
    'scripts/pricetag/metering_admin.py',
)


def mock_model():
    config=json.loads((sso.lab.STATE/'gateway-sso.json').read_text())
    endpoints=[endpoint for chain in config['filter_chains'] for filt in chain['filters']
               for cluster in filt.get('clusters',[]) for endpoint in cluster.get('endpoints',[])]
    if not endpoints or any(not isinstance(endpoint,str) or not endpoint.startswith(('127.0.0.1:','localhost:')) for endpoint in endpoints):
        raise ValueError('Inference probe requires local mock backends')
    if (sso.lab.STATE/'mock-profiles.json').exists():
        from mock_profiles import configuration
        if json.loads((sso.lab.STATE/'mock-profiles.json').read_text())!=configuration():
            raise ValueError('Mock profiles differ from reviewed fixtures')
        return 'openai/gpt-5.4-mini'
    # The minimal fixture's catalog alias is defined by prepare.py; the realistic
    # profiles above deliberately use the normal openai/external namespaces.
    return sso.prepare.mock_inputs()[1][0]['id']


def failure_details(error):
    # Never relay service responses, command arguments or credential-bearing stderr.
    if not isinstance(error,subprocess.CalledProcessError) or not isinstance(error.stderr,bytes):
        return 'details suppressed'
    match=re.search(rb'^PROBE_FAILURE (stage=(?:initialization|access-create|access-repeat|selected-key|loopback-api|inference-accounting|retirement|clear-keys) reason=(?:identity|users-response|login-mapping|budgets-response|budget-row|budget-value|existing-budget|access-write|budget|key-issuance|key-validation|expiry|key-storage|other) status=(?:0|[1-5][0-9]{2}))$',error.stderr,re.MULTILINE)
    return match[1].decode('ascii') if match else 'details suppressed'


def main(count=2):
    if type(count) is not int or not 2<=count<=10000:raise ValueError("Use 2–10000 synthetic users")
    info=json.loads(sso.run('pod','inspect',sso.lab.POD,timeout=60))[0]
    if info.get('Labels',{}).get('io.enmaas.test-lab')!=sso.lab.INSTANCE:
        raise ValueError('Not the selected local test pod')
    model=mock_model()
    payload=io.BytesIO()
    with tarfile.open(fileobj=payload,mode='w') as archive:
        for name in FILES:archive.add(sso.lab.ROOT/name,arcname=name,recursive=False)
    bootstrap="import tarfile,sys,runpy; tarfile.open(fileobj=sys.stdin.buffer,mode='r|').extractall('/tmp/probe',filter='data'); runpy.run_path('/tmp/probe/tests/external-services/population-api-probe.py',run_name='__main__')"
    values=dict(line.split('=',1) for line in (sso.lab.STATE/'gateway-sso.env').read_text().splitlines())
    management_name=sso.lab.secret_name('population-management-'+secrets.token_hex(6))
    client_name=sso.lab.POD+'-population-client-'+secrets.token_hex(6)
    try:
        sso.run('secret','create','--label',sso.lab.LABEL,management_name,'-',input=values['MANAGEMENT_API_SECRET'].encode(),timeout=60)
        output=sso.run('run','--rm','--name',client_name,'--interactive','--pull=never','--pod',sso.lab.POD,
        '--env','GATEWAY_PORT='+str(sso.lab.port('gateway')),'--env','PROBE_POPULATION_COUNT='+str(count),'--env','PROBE_MODEL='+model,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--memory','512m','--pids-limit','64','--tmpfs','/tmp:rw,noexec,nosuid,size=256m',
        '--secret',sso.lab.secret_name('gateway-sso-partner')+',target=/fixture/partner-secret,mode=0400',
        '--secret',sso.lab.secret_name('ca.pem')+',target=/fixture/ca.pem,mode=0444',
        '--secret',management_name+',target=/fixture/management-secret,mode=0400',
        sso.prepare.PYTHON,'python3','-B','-c',bootstrap,input=payload.getvalue(),timeout=min(3600,max(600,count*2)))
    finally:
        failures=[]
        for command in (('rm','--force','--ignore',client_name),('secret','rm',management_name)):
            try:sso.run(*command,timeout=60)
            except Exception:failures.append(command[0])
        if failures:raise ValueError('Temporary population client/credential cleanup incomplete; inspect labelled resources') from None
    print(output.decode().strip())


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users",type=int,default=2,help="synthetic population size, 2–10000; default smoke test: 2")
    try:main(parser.parse_args().users)
    except Exception as error:
        raise SystemExit('Local population probe failed; '+failure_details(error)+'; verify cleanup before another run.') from None
