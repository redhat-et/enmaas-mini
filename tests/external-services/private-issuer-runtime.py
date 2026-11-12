#!/usr/bin/env python3
"""Verify private hostname routing and TLS in disposable, unexposed containers.

The default HTTPS fixture returns an issuer document. With --keycloak, the pinned
issuer serves real discovery and exercises form login, PKCE exchange and userinfo
for both clients, then the pinned OAuth2 Proxy's sessions/group gate. Neither mode
qualifies PriceTag sessions, browser cookie policies or AWS routing.
"""
import importlib.util
import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]/'scripts/pricetag'))
import prepare
spec=importlib.util.spec_from_file_location('issuer_certs',HERE/'certificates.py')
certs=importlib.util.module_from_spec(spec);spec.loader.exec_module(certs)
spec=importlib.util.spec_from_file_location('private_issuer_lab',HERE/'local.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)

SERVER='''import http.server,json,ssl
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path!='/realms/test/.well-known/openid-configuration':
   self.send_error(404);return
  body=json.dumps({'issuer':'https://identity.lab.example.com:8443/realms/test'}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
server=http.server.HTTPServer(('0.0.0.0',8443),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/run/secrets/server.pem','/run/secrets/server.key')
server.socket=context.wrap_socket(server.socket,server_side=True)
server.serve_forever()
'''
CLIENT='''import json,ssl,urllib.request,urllib.error
context=ssl.create_default_context(cafile='/run/secrets/ca.pem')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
with opener.open('https://identity.lab.example.com:8443/realms/test/.well-known/openid-configuration',timeout=5) as reply:
 document=json.load(reply)
 assert document['issuer']=='https://identity.lab.example.com:8443/realms/test'
 for field in ('authorization_endpoint','token_endpoint','jwks_uri','end_session_endpoint'):
  if field in document:assert document[field].startswith(document['issuer']+'/')
try:
 opener.open('https://wrong.lab.example.com:8443/realms/test/.well-known/openid-configuration',timeout=5)
except urllib.error.URLError as error:
 assert isinstance(error.reason,ssl.SSLCertVerificationError)
else:raise AssertionError('wrong hostname accepted')
print('PASS: private hostname connection and certificate validation; wrong hostname rejected')
'''


def command(*args,input=None,check=True):
    result=subprocess.run(['podman',*args],input=input,capture_output=True,text=True,timeout=60)
    if check and result.returncode:raise RuntimeError('Private issuer runtime operation failed; output suppressed')
    return result


def main(keycloak=False):
    name='enmaas-issuer-path-'+secrets.token_hex(6)
    label='io.enmaas.test-lab='+name
    created=[];network=False;container=False;proxy=False
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix=name+'-') as temporary:
        root=Path(temporary)
        certs.generate(root/'certs',{'gateway':['192.0.2.10'],'keycloak':['192.0.2.11'],
            'observability':['192.0.2.12']},1,dns_names={'keycloak':'identity.lab.example.com',
                'observability':'observability.lab.example.com'})
        if keycloak:
            lab.configure(root/'realm-inputs',certificates=False)
        material={'server.py':SERVER,'server.pem':(root/'certs/keycloak/server.pem').read_text(),
            'server.key':(root/'certs/keycloak/server.key').read_text(),
            'ca.pem':(root/'certs/trust/issuer-ca.pem').read_text()}
        if keycloak:
            realm=json.loads((root/'realm-inputs/realm.json').read_text())
            realm['clients'][0]['redirectUris'].append('https://observability.lab.example.com:19443/oauth2/callback')
            realm['clients'][0]['webOrigins'].append('https://observability.lab.example.com:19443')
            material['realm.json']=json.dumps(realm)
            proxy_config=(root/'realm-inputs/oauth.cfg').read_text().replace(
                'https://localhost:18443','https://identity.lab.example.com:8443').replace(
                'https://localhost:19443','https://observability.lab.example.com:19443').replace(
                'http://127.0.0.1:8080/','static://202')
            material.update({'oauth.cfg':proxy_config,
                'proxy.pem':(root/'certs/observability/server.pem').read_text(),
                'proxy.key':(root/'certs/observability/server.key').read_text(),
                'browser-ca.pem':material['ca.pem'],
                'issuer_client.py':(HERE/'private-issuer-client.py').read_text()})
            for suffix in ('credentials.json','identities.json'):
                material[suffix]=(root/'realm-inputs'/suffix).read_text()
        try:
            command('network','create','--internal','--label',label,name);network=True
            for suffix,value in material.items():
                secret=name+'-'+suffix
                command('secret','create','--label',label,secret,'-',input=value);created.append(secret)
            options=['--pull=never','--network',name,'--read-only','--cap-drop','ALL',
                     '--security-opt','no-new-privileges','--user','1001:1001','--memory','128m','--pids-limit','64']
            mounts=[]
            for suffix in ('server.py','server.pem','server.key'):
                mounts+=['--secret',name+'-'+suffix+',target='+suffix+',mode=0444']
            container=True
            if keycloak:
                server_options=[*options]
                server_options.remove('--read-only')  # Keycloak start-dev builds its disposable runtime.
                # Match the pinned image's non-root owner of /opt/keycloak.
                server_options[server_options.index('1001:1001')]='1000:0'
                server_options[server_options.index('128m')]='1536m'
                server_options[server_options.index('64')]='256'
                command('run','--detach','--name',name,'--label',label,*server_options,*mounts,
                    '--tmpfs','/opt/keycloak/data:rw,nosuid,nodev,mode=1777,size=256m',
                    '--tmpfs','/tmp:rw,nosuid,nodev,size=64m',
                    '--secret',name+'-realm.json,target=/opt/keycloak/data/import/realm.json,mode=0444',
                    lab.IMAGES['keycloak'],'start-dev','--http-enabled=false','--https-port=8443',
                    '--https-certificate-file=/run/secrets/server.pem',
                    '--https-certificate-key-file=/run/secrets/server.key',
                    '--hostname=https://identity.lab.example.com:8443','--import-realm')
            else:
                command('run','--detach','--name',name,'--label',label,*options,*mounts,
                        prepare.PYTHON,'python3','-B','/run/secrets/server.py')
            details=json.loads(command('inspect',name).stdout)[0]
            if any(details['NetworkSettings'].get('Ports',{}).values()):raise RuntimeError('unexpected public port')
            private=details['NetworkSettings']['Networks'][name]['IPAddress']
            if not ipaddress.ip_address(private).is_private:raise RuntimeError('network is not private')
            if not json.loads(command('network','inspect',name).stdout)[0]['internal']:
                raise RuntimeError('test network permits external traffic')
            client_program=CLIENT.replace('/realms/test','/realms/enmaas-lab') if keycloak else CLIENT
            if keycloak:
                client_program=client_program.replace(" for field in (", " assert all(field in document for field in ('authorization_endpoint','token_endpoint','jwks_uri','end_session_endpoint'))\n for field in (")
            for _ in range(40):
                if not json.loads(command('inspect',name).stdout)[0]['State']['Running']:
                    raise RuntimeError('disposable issuer exited before readiness; no client request can qualify it')
                result=command('run','--rm','--interactive',*options,
                    '--add-host','identity.lab.example.com:'+private,
                    '--add-host','wrong.lab.example.com:'+private,
                    '--secret',name+'-ca.pem,target=ca.pem,mode=0444',
                    prepare.PYTHON,'python3','-B','-',input=client_program,check=False)
                if result.returncode==0:break
                time.sleep(1)
            else:
                # This client has only generated test hostnames and a public CA;
                # never print issuer logs or the imported realm credentials.
                raise RuntimeError('private issuer readiness or trust checks failed: '+result.stderr[-3000:])
            print(result.stdout.strip())
            if keycloak:
                print('PASS: pinned Keycloak discovery and protocol endpoints preserve the canonical DNS issuer')
                private_mounts=[]
                for suffix in ('credentials.json','identities.json'):
                    private_mounts+=['--secret',name+'-'+suffix+',target='+suffix+',uid=1001,mode=0400']
                result=command('run','--rm','--interactive',*options,
                    '--add-host','identity.lab.example.com:'+private,
                    '--secret',name+'-ca.pem,target=ca.pem,mode=0444',*private_mounts,
                    prepare.PYTHON,'python3','-B','-',
                    input=(HERE/'private-issuer-client.py').read_text())
                print(result.stdout.strip())
                proxy_mounts=[]
                for suffix,target in (('oauth.cfg','/lab/oauth.cfg'),('proxy.pem','/lab/server.pem'),
                                      ('proxy.key','/lab/server.key'),('ca.pem','/lab/ca.pem')):
                    proxy_mounts+=['--secret',name+'-'+suffix+',target='+target+',uid=1001,mode=0400']
                proxy=True
                command('run','--detach','--name',name+'-proxy','--label',label,*options,
                    '--add-host','identity.lab.example.com:'+private,*proxy_mounts,
                    lab.IMAGES['auth'],'--config=/lab/oauth.cfg')
                details=json.loads(command('inspect',name+'-proxy').stdout)[0]
                if any(details['NetworkSettings'].get('Ports',{}).values()):raise RuntimeError('unexpected proxy port publication')
                proxy_ip=details['NetworkSettings']['Networks'][name]['IPAddress']
                proxy_client=['run','--rm','--interactive',*options,
                    '--add-host','observability.lab.example.com:'+proxy_ip,
                    '--add-host','identity.lab.example.com:'+private,
                    '--secret',name+'-browser-ca.pem,target=browser-ca.pem,mode=0444',
                    '--secret',name+'-issuer_client.py,target=issuer_client.py,mode=0444',*private_mounts,
                    prepare.PYTHON,'python3','-B','-']
                readiness='''import ssl,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile='/run/secrets/browser-ca.pem')))
with opener.open('https://observability.lab.example.com:19443/ping',timeout=3) as response:
 assert response.status==200
'''
                for _ in range(20):
                    result=command(*proxy_client,input=readiness,check=False)
                    if result.returncode==0:break
                    if not json.loads(command('inspect',name+'-proxy').stdout)[0]['State']['Running']:
                        raise RuntimeError('private proxy exited')
                    time.sleep(1)
                else:raise RuntimeError('private proxy readiness failed')
                # Do not retry an authorization failure after readiness succeeds.
                result=command(*proxy_client,input=(HERE/'private-proxy-client.py').read_text())
                print(result.stdout.strip())
        finally:
            if proxy:command('rm','--force',name+'-proxy')
            if container:command('rm','--force',name)
            for secret in reversed(created):command('secret','rm',secret)
            if network:command('network','rm',name)
    print('PASS: no ports published; disposable containers, secrets, network and signing keys removed.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keycloak',action='store_true',help='use the pinned real issuer with a disposable development database')
    main(parser.parse_args().keycloak)
