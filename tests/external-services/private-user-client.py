#!/usr/bin/env python3
"""Disposable issuer-only provisioning contract; never targets a remote deployment."""
import json
import os
import time
import importlib.util
import tempfile
from pathlib import Path
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE='https://identity.lab.example.com:8443'
USERS='/admin/realms/enmaas-lab/users'
PROGRESS={'stage':'initial','created':0,'verified':0,'deleted':0,'errno':None}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Unexpected redirect')


def main():
    context=ssl.create_default_context(cafile='/run/secrets/ca.pem')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())
    credentials=json.loads(Path('/run/secrets/credentials.json').read_text())
    def request(path,method='GET',token=None,body=None,form=False,expected=200):
        headers={'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json'}
        if token:headers['Authorization']='Bearer '+token
        data=(urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()) if body is not None else None
        try:
            with opener.open(urllib.request.Request(BASE+path,data=data,headers=headers,method=method),timeout=10) as reply:
                status=reply.status;location=reply.headers.get('Location');raw=reply.read(65537)
        except urllib.error.HTTPError as error:
            status=error.code;location=None;raw=b''
        if status!=expected or len(raw)>65536:raise ValueError('Unexpected provisioning API response')
        return (json.loads(raw) if raw else None),location
    token=request('/realms/enmaas-lab/protocol/openid-connect/token',method='POST',form=True,body={
        'grant_type':'client_credentials','client_id':'lab-authorization-test',
        'client_secret':credentials['authorization_test']})[0]['access_token']
    name='bulk-probe-'+secrets.token_hex(8)
    profile={'username':name,'email':name+'@example.test','enabled':True,'emailVerified':True,
        'firstName':'Bulk','lastName':'Probe','groups':[],
        'credentials':[{'type':'password','value':secrets.token_urlsafe(32),'temporary':False}]}
    request(USERS,method='POST',body=profile,expected=401)
    _,location=request(USERS,method='POST',token=token,body=profile,expected=201)
    prefix=BASE+USERS+'/'
    if not isinstance(location,str) or not location.startswith(prefix):raise ValueError('Unexpected created-user location')
    identity=location[len(prefix):]
    if str(uuid.UUID(identity))!=identity:raise ValueError('Invalid user UUID')
    path=USERS+'/'+identity
    try:
        first=request(path,token=token)[0]
        second=request(path,token=token)[0]
        if first['id']!=identity or second['id']!=identity or first['username']!=name:
            raise ValueError('User identity changed')
        request(USERS,method='POST',token=token,body=profile,expected=409)
        found=request(USERS+'?'+urllib.parse.urlencode({'username':name,'exact':'true'}),token=token)[0]
        if len(found)!=1 or found[0]['id']!=identity:raise ValueError('Duplicate creation changed identity')
        groups=request(path+'/groups',token=token)[0]
        roles=request(path+'/role-mappings',token=token)[0]
        if groups or roles.get('clientMappings',{}).get('realm-management'):
            raise ValueError('Synthetic user gained privileged group or administration roles')
    finally:
        request(path,method='DELETE',token=token,expected=204)
    request(path,token=token,expected=404)
    print('PASS: anonymous create denied; server UUID stable; duplicate username rejected; ordinary membership; user deleted')
    spec=importlib.util.spec_from_file_location('population','/run/secrets/user-population.py')
    population=importlib.util.module_from_spec(spec);spec.loader.exec_module(population)
    transport_spec=importlib.util.spec_from_file_location('transport','/run/secrets/populate-keycloak.py')
    transport=importlib.util.module_from_spec(transport_spec);transport_spec.loader.exec_module(transport)
    admin=transport.Issuer('/run/secrets/ca.pem',{'grant_type':'client_credentials',
        'client_id':'lab-authorization-test','client_secret':credentials['authorization_test']},
        origin=BASE,token_realm='enmaas-lab')
    count=int(os.environ.get('ENMAAS_POPULATION_COUNT','2'))
    if not 1<=count<=10000:raise ValueError('Invalid population size')
    created=[];timings={}
    def create_user(profile):
        uid=admin.create_user(profile);created.append(uid);PROGRESS['created']=len(created);return uid
    def inspect_user(uid):
        result=admin.inspect_user(uid)
        if request(USERS+'/'+uid+'/groups',token=admin.access_token())[0]:raise ValueError('Unexpected group')
        PROGRESS['verified']+=1
        return result
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'population';population.initialize(root,count)
            started=time.monotonic()
            PROGRESS['stage']='create'
            first=population.run(root,create_user,inspect_user)
            timings['create_seconds']=round(time.monotonic()-started,3);started=time.monotonic()
            PROGRESS.update(stage='verify',verified=0)
            second=population.run(root,create_user,inspect_user)
            timings['verify_seconds']=round(time.monotonic()-started,3)
            if first!=second or len(created)!=count:raise ValueError('Resume recreated identities')
            PROGRESS['stage']='export'
            exported=population.export_profiles(root)
            if [profile['user_id'] for profile in exported['profiles']]!=created:
                raise ValueError('Export changed issuer UUIDs')
            private_spec=importlib.util.spec_from_file_location('saved_password','/run/secrets/keycloak-password.py')
            saved=importlib.util.module_from_spec(private_spec);private_spec.loader.exec_module(saved)
            for index in sorted({1,count}):
                password=saved.population_password(root,index)
                record=population.load(root,str(index).zfill(5)+'.json')
                if password!=record['password'] or password in json.dumps(exported):
                    raise ValueError('Selected credential retrieval or credential-free export differs')

    finally:
        failed_stage=PROGRESS['stage']
        PROGRESS['stage']='delete'
        for uid in created:
            request(USERS+'/'+uid,method='DELETE',token=admin.access_token(),expected=204)
            PROGRESS['deleted']+=1
        PROGRESS['stage']=failed_stage
    print('PASS: issuer population created; resume/export preserved UUIDs; selected passwords absent from export; all created users deleted')
    print(json.dumps({'users':count,**timings,'issuer_cleanup_complete':True}))



if __name__=='__main__':
    try:main()
    except Exception as error:
        PROGRESS['errno']=error.errno if isinstance(error,OSError) and type(error.errno) is int else None
        print(json.dumps(PROGRESS),flush=True)
        raise SystemExit('Disposable user API probe failed; credential-bearing diagnostics suppressed') from None
