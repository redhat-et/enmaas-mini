#!/usr/bin/env python3
"""Prepare the dedicated RHEL test issuer; no cloud changes or public admin API."""
import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import secrets
import tempfile

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('local_lab',HERE/'local.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
spec=importlib.util.spec_from_file_location('issuer_identity',HERE/'issuer-config.py')
identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity)
PRAXIS='quay.io/opendatahub/praxis-experimental@sha256:502f120fed7b8e516ce1d78396045c8f72673672351432b9dc2e61baf5c13ac8'


def address(value):
    try:return str(ipaddress.IPv4Address(value))
    except ValueError:raise ValueError('the test lab requires explicit IPv4 addresses') from None


def configure(root,issuer,gateway,observability,*,issuer_name=None,issuer_private_ip=None):
    issuer,gateway,observability=map(address,(issuer,gateway,observability))
    issuer,mapping=identity.resolve(issuer,issuer_name,issuer_private_ip)
    if any(root.iterdir()):raise FileExistsError('use a new empty private output directory')
    root.chmod(0o700)
    # Reuse the tested realm/identity generator; only copy issuer-owned inputs.
    with tempfile.TemporaryDirectory() as temporary:
        source=Path(temporary);lab.configure(source,certificates=False)
        for name in ('realm.json','credentials.json','identities.json','db-password'):
            lab.write(root,name,(source/name).read_text())
    credentials=json.loads((root/'credentials.json').read_text())
    lab.write(root,'observability-client-secret',credentials['oidc'])
    credentials['bootstrap_admin']=secrets.token_urlsafe(32)
    lab.write(root,'credentials.json',credentials)
    realm=json.loads((root/'realm.json').read_text())
    for client in realm['clients']:
        target=observability if client['clientId']=='observability' else gateway
        callback='/oauth2/callback' if client['clientId']=='observability' else '/oauth/callback'
        client['redirectUris']=[f'https://{target}:8443{callback}']
        client['webOrigins']=[f'https://{target}:8443']
        if client['clientId']=='enmaas-mini':
            client['attributes']['post.logout.redirect.uris']=f'https://{target}:8443/login'
    lab.write(root,'realm.json',realm)
    lab.write(root,'keycloak.conf',f'''db=postgres
cache=local
db-url=jdbc:postgresql://lab-keycloak-db:5432/keycloak
db-username=keycloak
db-password={credentials['db']}
hostname=https://{issuer}:8443
hostname-admin=https://localhost:18443
http-enabled=true
http-port=8080
https-port=18443
https-certificate-file=/run/secrets/lab-server-cert
https-certificate-key-file=/run/secrets/lab-server-key
bootstrap-admin-username=lab-bootstrap
bootstrap-admin-password={credentials['bootstrap_admin']}
''')
    lab.write(root,'gateway-oidc.json',{'issuer_url':f'https://{issuer}:8443/realms/enmaas-lab',
        'client_id':'enmaas-mini','client_secret':credentials['gateway_oidc'],
        'ca_file':'/root/enmaas-inputs/issuer-ca.pem',
        **({'issuer_private_ip':mapping.split(':',1)[1]} if mapping else {})})
    proxy={'admin':{'address':'127.0.0.1:9901'},
        'insecure_options':{'allow_private_endpoints':True,'allow_private_upstreams':True},
        'listeners':[{'name':'issuer','address':'0.0.0.0:8443','filter_chains':['issuer'],
            'tls':{'certificates':[{'cert_path':'/run/secrets/lab-server-cert','key_path':'/run/secrets/lab-server-key'}]}}],
        'filter_chains':[{'name':'issuer','filters':[
            {'filter':'static_response','status':404,'body':'Route not available',
             'conditions':[{'unless':{'path_prefix':p}} for p in ('/realms/enmaas-lab/','/resources/')]},
            {'filter':'rate_limit','mode':'global','rate':30,'burst':120},
            {'filter':'router','routes':[{'path_prefix':'/','cluster':'issuer'}]},
            {'filter':'load_balancer','clusters':[{'name':'issuer','endpoints':['lab-keycloak:8080']}]}]}]}
    lab.write(root,'proxy.json',proxy)
    units=root/'quadlets';units.mkdir(mode=0o700)
    lab.write(units,'lab-keycloak.network','[Network]\nNetworkName=enmaas-lab-keycloak\n')
    lab.write(units,'lab-keycloak-db.volume','[Volume]\nVolumeName=enmaas-lab-keycloak-db\n')
    common='Network=lab-keycloak.network\nNoNewPrivileges=true\nDropCapability=all\nPidsLimit=256\n'
    end='\n[Service]\nRestart=on-failure\nTimeoutStartSec=300\n[Install]\nWantedBy=default.target\n'
    lab.write(units,'lab-keycloak-db.container','[Container]\nImage='+lab.IMAGES['db']+'\n'+common+'''
ContainerName=lab-keycloak-db
User=999:999
Environment=POSTGRES_USER=keycloak POSTGRES_DB=keycloak POSTGRES_PASSWORD_FILE=/run/secrets/lab-db-password
Secret=lab-db-password
Volume=lab-keycloak-db.volume:/var/lib/postgresql/data:U
''' + end.replace('[Service]','[Service]\nMemoryMax=1G'))
    tls='Secret=lab-server-cert\nSecret=lab-server-key\n'
    lab.write(units,'lab-keycloak.container','[Unit]\nRequires=lab-keycloak-db.service\nAfter=lab-keycloak-db.service\n[Container]\nImage='+lab.IMAGES['keycloak']+'\n'+common+tls+'''
ContainerName=lab-keycloak
PublishPort=127.0.0.1:18443:18443
Secret=lab-keycloak-config,target=/lab/keycloak.conf
Secret=lab-keycloak-realm,target=/opt/keycloak/data/import/realm.json
Exec=--config-file=/lab/keycloak.conf start --import-realm
''' + end.replace('[Service]','[Service]\nMemoryMax=2G'))
    lab.write(units,'lab-keycloak-proxy.container','[Unit]\nRequires=lab-keycloak.service\nAfter=lab-keycloak.service\n[Container]\nImage='+PRAXIS+'\n'+common+tls+'''
ContainerName=lab-keycloak-proxy
PublishPort=8443:8443
Secret=lab-keycloak-proxy-config,target=/etc/praxis/config.json
Exec=-c /etc/praxis/config.json
''' + end.replace('[Service]','[Service]\nMemoryMax=512M'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('issuer','gateway','observability'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--issuer-name',help='canonical issuer DNS name; requires --issuer-private-ip')
    parser.add_argument('--issuer-private-ip',help='RFC1918 issuer address for server backchannels')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    configure(args.output,args.issuer,args.gateway,args.observability,
              issuer_name=args.issuer_name,issuer_private_ip=args.issuer_private_ip)
    print('Private issuer inputs prepared; no services started. Import a matching TLS certificate before installation.')

if __name__=='__main__':main()
