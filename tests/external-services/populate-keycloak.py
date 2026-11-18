#!/usr/bin/env python3
"""Populate synthetic issuer users through local verified HTTPS; no PriceTag mapping yet."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('keycloak_population',HERE/'user-population.py')
population=importlib.util.module_from_spec(spec);spec.loader.exec_module(population)
spec=importlib.util.spec_from_file_location('keycloak_private_state',HERE/'keycloak-password.py')
private=importlib.util.module_from_spec(spec);spec.loader.exec_module(private)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Issuer redirects are forbidden')


class Issuer:
    """The hostname override is for the isolated runtime test, not a CLI input."""
    def __init__(self,ca,form,*,origin='https://localhost:18443',token_realm='master'):
        if origin not in ('https://localhost:18443','https://identity.lab.example.com:8443') or token_realm not in ('master','enmaas-lab'):
            raise ValueError('Unsupported administration endpoint')
        self.origin=origin;self.form=form;self.realm=token_realm;self.token=None;self.expires=0
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))),NoRedirect())

    def request(self,path,method='GET',body=None,*,form=False,expected=200,token=None):
        data=(urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()) if body is not None else None
        headers={'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json'}
        if token:headers['Authorization']='Bearer '+token
        try:
            with self.opener.open(urllib.request.Request(self.origin+path,data=data,headers=headers,method=method),timeout=30) as reply:
                if reply.status!=expected:
                    raise ValueError('Unexpected status')
                raw=reply.read(65537)
                if len(raw)>65536:
                    raise ValueError('Oversized response')
                return (json.loads(raw) if raw else None),reply.headers.get('Location')
        except (OSError,ValueError):
            raise ValueError('Issuer API request failed; credential-bearing diagnostics suppressed, no automatic mutation retry') from None

    def access_token(self):
        if self.token is None or time.monotonic()>=self.expires:
            start=time.monotonic()
            result,_=self.request('/realms/'+self.realm+'/protocol/openid-connect/token','POST',self.form,form=True)
            if (not isinstance(result,dict) or not isinstance(result.get('access_token'),str)
                    or not 1<=len(result['access_token'])<=16384
                    or any(ord(c)<33 or ord(c)>126 for c in result['access_token'])
                    or type(result.get('expires_in')) is not int or not 5<result['expires_in']<=3600):
                raise ValueError('Invalid issuer token response')
            self.token=result['access_token'];self.expires=start+result['expires_in']-5
        return self.token

    def create_user(self,profile):
        _,location=self.request('/admin/realms/enmaas-lab/users','POST',profile,token=self.access_token(),expected=201)
        prefix=self.origin+'/admin/realms/enmaas-lab/users/'
        if not isinstance(location,str) or not location.startswith(prefix):raise ValueError('Unexpected user location')
        identifier=location[len(prefix):]
        if str(uuid.UUID(identifier))!=identifier:raise ValueError('Invalid created UUID')
        return identifier

    def inspect_user(self,identifier):
        if not isinstance(identifier,str) or str(uuid.UUID(identifier))!=identifier:raise ValueError('Invalid UUID')
        return self.request('/admin/realms/enmaas-lab/users/'+identifier,token=self.access_token())[0]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,default=Path('/root/enmaas-keycloak-inputs'))
    parser.add_argument('--state',type=Path,default=Path('/root/enmaas-test-users'))
    parser.add_argument('--ca',type=Path,default=Path('/root/enmaas-tls/ca.pem'))
    parser.add_argument('--count',type=int,default=10000)
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--export',type=Path,help='new root-private identity-profile file; excludes passwords')
    args=parser.parse_args()
    if os.geteuid()!=0 or not args.yes:raise ValueError('Run on the issuer host with sudo and explicit --yes')
    if not 1<=args.count<=10000:raise ValueError('Choose 1–10000 synthetic users')
    credentials=private.read(args.inputs/'credentials.json')
    password=credentials.get('bootstrap_admin')
    if not isinstance(password,str) or not password or len(password)>1024:raise ValueError('No saved bootstrap administrator credential')
    client=Issuer(args.ca,{'grant_type':'password','client_id':'admin-cli','username':'lab-bootstrap','password':password})
    # Authenticate before allocating a population; failure leaves no new records.
    client.access_token()
    if args.state.exists() or args.state.is_symlink():
        if population.load(args.state,'population.json')['count']!=args.count:
            raise ValueError('Population count differs; do not recreate existing identities')
    else:population.initialize(args.state,args.count)
    result=population.run(args.state,client.create_user,client.inspect_user)
    if args.export:
        directory=population.secure_dir(args.export.parent);os.close(directory)
        population.save(args.export.parent,args.export.name,population.export_profiles(args.state),exclusive=True)
        result['identity_export']=str(args.export)
    print(json.dumps({**result,'private_state':str(args.state)},indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        raise SystemExit('Population stopped; preserve private records and reconcile uncertain creation before retrying. No credential values printed.') from None
