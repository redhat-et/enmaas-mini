"""Fixture-only exercise of the real import engine and PriceTag partner API."""
import importlib.util
import json
from pathlib import Path
import secrets
import os
import ssl
import time
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid


STAGE="initialization"
STATUS=0

def main():
    global STAGE,STATUS
    spec=importlib.util.spec_from_file_location('population_import',Path(__file__).with_name('import-population.py'))
    importer=importlib.util.module_from_spec(spec);spec.loader.exec_module(importer)
    importer.oidc.DEPLOY=Path('/fixture')
    gateway_port=int(os.environ.get("GATEWAY_PORT","28443"))
    count=int(os.environ.get("PROBE_POPULATION_COUNT","2"))
    if not 2<=count<=10000:raise ValueError("Invalid probe population size")
    started=time.monotonic();timings={}
    original=urllib.request.Request
    # The shared local pod uses 8087; the installed RHEL host uses 18087.
    def request(url,*args,**kwargs):
        if not url.startswith('http://127.0.0.1:18087/api/v1/users/'):
            raise ValueError('Probe attempted a non-fixture destination')
        return original(url.replace(':18087/',':8087/',1),*args,**kwargs)
    importer.oidc.urllib.request.Request=request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),importer.oidc.NoRedirect())
    secret=Path('/fixture/partner-secret').read_text().strip()
    prefix='perf-'+secrets.token_hex(8)
    profiles=[{'user_id':str(uuid.uuid4()),'tags':{'email':prefix+'-'+str(i).zfill(5)+'@example.test',
        'first_name':'Synthetic','last_name':'User'}} for i in range(1,count+1)]
    document={'version':1,'population':prefix,'profiles':profiles}
    attempted=[];retired=False;untracked_owner=None;all_revoked=False
    def provision(profile):
        attempted.append(profile['user_id'])
        return importer.oidc.provision(profile)
    def api(identity,method='GET',authorized=True,body=None):
        headers={'Authorization':'Bearer '+secret} if authorized else {}
        if body is not None:headers['Content-Type']='application/json'
        with opener.open(request('http://127.0.0.1:18087/api/v1/users/'+identity,
                                method=method,headers=headers,data=json.dumps(body).encode() if body is not None else None),timeout=15) as response:
            return json.load(response)
    def management(method,resource,body=None):
        if resource not in ('users','key-revocations','budgets','keys'):raise ValueError('Unexpected management resource')
        token=Path('/fixture/management-secret').read_text().strip()
        req=original('http://127.0.0.1:8087/api/v1/management/'+resource,
            data=json.dumps(body).encode() if body is not None else None,method=method,
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        global STATUS
        try:
            with opener.open(req,timeout=15) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            STATUS=error.code;raise
    def revoke_all(subject,dry_run):
        return management('POST','key-revocations',{'subject':subject,'dry_run':dry_run})
    try:
        try:api(profiles[0]['user_id'],authorized=False)
        except urllib.error.HTTPError as error:
            if error.code!=401:raise ValueError('Unexpected anonymous status') from None
        else:raise ValueError('Anonymous identity read allowed')
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary)/'receipts'
            first=importer.apply(document,state,provision)
            second=importer.apply(document,state,provision)
            if first!=second or len(attempted)!=count:raise ValueError('Confirmed import was replayed')
            for profile in profiles:
                actual=api(profile['user_id'])
                if actual.get('user_id')!=profile['user_id'] or actual.get('active') is not True:
                    raise ValueError('Identity mapping differs')
            timings['identity_import_and_repeat_seconds']=round(time.monotonic()-started,3)
            access_started=time.monotonic()
            STAGE='access-create'
            access=importer.configure_access(document,state,management)
            if access!={'configured_users':count,'monthly_usd':5,'key_lifetime':'24h'}:
                raise ValueError('Population access setup did not complete')
            STAGE='access-repeat'
            if importer.configure_access(document,state,management)!=access:
                raise ValueError('Population access resume differed')
            timings['access_create_and_repeat_seconds']=round(time.monotonic()-access_started,3)
            STAGE='selected-key'
            spec=importlib.util.spec_from_file_location('selected_population_key',Path(__file__).with_name('population-key.py'))
            selected=importlib.util.module_from_spec(spec);spec.loader.exec_module(selected)
            info,raw=selected.select(document,state,1,lambda profile:api(profile['user_id']),importer.oidc.list_keys)
            if info['user_id']!=profiles[0]['user_id'] or not raw.endswith(b'\n'):
                raise ValueError('Selected key export differs')
            STAGE='loopback-api'
            api_client=Path(__file__).resolve().parents[2]/'scripts/pricetag/api'
            key_path=state/(profiles[0]['user_id']+'.key')
            def public_call(name,route,success,body=None):
                output=Path(temporary)/(name+'.json')
                extra=[]
                if body is not None:
                    source=Path(temporary)/(name+'-input.json')
                    source.touch(mode=0o600);source.write_text(json.dumps(body))
                    extra=['--input',str(source)]
                result=subprocess.run([sys.executable,str(api_client),'PATCH' if body is not None else 'GET',route,
                    '--url',f'https://127.0.0.1:{gateway_port}','--origin',f'https://localhost:{gateway_port}',
                    '--key-file',str(key_path),'--ca-file','/fixture/ca.pem',
                    '--output',str(output),*extra],capture_output=True,timeout=90)
                if success:
                    if result.returncode!=0:raise ValueError('Loopback authenticated API failed')
                    return json.loads(output.read_text())
                if result.returncode==0 or b'API returned HTTP 403;' not in result.stderr:
                    raise ValueError('Loopback role denial failed')
            public_call('ordinary-self','/api/v1/whoami',True)
            STAGE='inference-accounting'
            baseline=public_call('usage-before','/api/v1/me/usage/summary',True)
            if baseline.get('overview',{}).get('total_requests')!=0:
                raise ValueError('New identity unexpectedly has usage')
            tls=ssl.create_default_context(cafile='/fixture/ca.pem')
            inference=urllib.request.build_opener(urllib.request.ProxyHandler({}),importer.oidc.NoRedirect(),urllib.request.HTTPSHandler(context=tls))
            req=original(f'https://127.0.0.1:{gateway_port}/v1/chat/completions',
                data=json.dumps({'model':os.environ['PROBE_MODEL'],'messages':[{'role':'user','content':'Synthetic identity accounting check'}],'max_tokens':8}).encode(),
                headers={'Authorization':'Bearer '+raw.decode().strip(),'Content-Type':'application/json'},method='POST')
            with inference.open(req,timeout=30) as response:reply=json.load(response)
            if reply.get('usage')!={'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}:
                raise ValueError('Unexpected mock usage response')
            for attempt in range(10):
                own=public_call('usage-after-'+str(attempt),'/api/v1/me/usage/summary',True)
                if own.get('overview',{}).get('total_requests')==1:break
                time.sleep(1)
            else:raise ValueError('Inference usage did not reach the selected identity')
            if own['overview'].get('total_tokens')!=5:raise ValueError('Incorrect identity token accounting')
            key_path=state/(profiles[1]['user_id']+'.key')
            other=public_call('other-user-usage','/api/v1/me/usage/summary',True)
            if other.get('overview',{}).get('total_requests')!=0:
                raise ValueError('Other identity received selected user usage')
            key_path=state/(profiles[0]['user_id']+'.key')
            STAGE='loopback-api'

            public_call('ordinary-admin-denied','/api/v1/management/users',False)
            member_route='/api/v1/admin/partner-users/'+profiles[1]['user_id']
            assignment={'role':'user','manager_user_id':profiles[0]['user_id']}
            public_call('ordinary-assignment-denied',member_route,False,assignment)
            management('PUT','users',{'subject':profiles[0]['tags']['email'],'role':'super_admin'})
            try:
                public_call('promoted-admin','/api/v1/management/users',True)
                assigned=public_call('assign-manager',member_route,True,assignment)
                if assigned.get('user_id')!=profiles[1]['user_id'] or assigned.get('manager_user_id')!=profiles[0]['user_id'] or assigned.get('role')!='user':
                    raise ValueError('Manager assignment contract differs')
                cleared=public_call('clear-manager',member_route,True,{'role':'user','manager_user_id':None})
                if cleared.get('manager_user_id') is not None:raise ValueError('Manager assignment not cleared')
            finally:
                management('PUT','users',{'subject':profiles[0]['tags']['email'],'role':'user'})
            public_call('demoted-admin-denied','/api/v1/management/users',False)
            STAGE='retirement'
            key=api(profiles[0]['user_id']+'/keys',method='POST',body={'name':'population-retirement-probe'})
            if not key.get('id') or not key.get('key'):raise ValueError('Partner key was not issued')
            key_id=key['id'];key.clear()
            untracked_owner=profiles[0]['tags']['email']
            req=original('http://127.0.0.1:8088/v1/api-keys',
                data=json.dumps({'name':'untracked-cleanup-probe','expiresIn':'1h'}).encode(),method='POST',
                headers={'Content-Type':'application/json','X-MaaS-Username':untracked_owner,
                         'X-MaaS-Group':'["system:authenticated"]'})
            with opener.open(req,timeout=15) as response:untracked=json.load(response)
            if not untracked.get('id') or not untracked.get('key'):raise ValueError('Untracked key was not created')
            untracked_id=untracked['id'];untracked.clear()
            revoked=[]
            def deactivate(profile):
                result=importer.oidc.deactivate(profile);revoked.append(result['keys_revoked']);return result
            importer.apply(document,state,deactivate,retire=True)
            importer.apply(document,state,deactivate,retire=True)
            if revoked!=[2]+[1]*(count-1):raise ValueError('Retirement did not revoke the tracked key exactly once')
            for profile in profiles:
                if api(profile['user_id']).get('active') is not False:raise ValueError('User still active')
            keys=api(profiles[0]['user_id']+'/keys')['keys']
            matching=[row for row in keys if row['id']==key_id]
            if matching and any(row.get('status','').lower()!='revoked' for row in matching):
                raise ValueError('Tracked key remains usable')
            retired=True
            leftover=[row for row in keys if row['id']==untracked_id]
            if len(leftover)!=1 or leftover[0].get('status','').lower()!='active':
                raise ValueError('Expected non-partner key was not left active by partner retirement')
            STAGE='clear-keys'
            preview=importer.clear_keys(document,state,management)
            if preview!={'users_checked':count,'active_keys':1,'changed':False}:
                raise ValueError('Population preview did not identify the remaining key')
            cleared=importer.clear_keys(document,state,management,execute=True)
            if cleared!={'users_checked':count,'active_keys':0,'changed':True}:
                raise ValueError('Population cleanup did not verify zero remaining keys')
            repeated=importer.clear_keys(document,state,management,execute=True)
            if repeated!={'users_checked':count,'active_keys':0,'changed':False}:
                raise ValueError('Completed population cleanup was replayed')
            all_revoked=True
    finally:
        if untracked_owner is not None and not all_revoked:revoke_all(untracked_owner,False)
        # Retire only UUIDs this invocation attempted. API deletion is soft deletion;
        # historical identity/audit records remain in the disposable lab database.
        for identity in ([] if retired else attempted):
            result=api(identity,method='DELETE')
            if result.get('active') is not False or type(result.get('keys_revoked')) is not int:
                raise ValueError('Synthetic identity retirement failed')
        for profile in profiles:
            if profile['user_id'] in attempted:
                management('DELETE','budgets',{'subject':profile['tags']['email']})
    print(json.dumps({'users':count,'elapsed_seconds':round(time.monotonic()-started,3),
        **timings,'accounting_verified':True,'keys_retired':all_revoked,'identities_retired':retired,'budgets_removed':True}))
    print('PASS: UUID import; nonprivileged role and USD budgets; finite private keys, selected-key export, mock inference with identity-scoped accounting and no-write resume; verified loopback login, team assignment and current-role denial; identity retirement; partner key revoked while untracked key remained; management dry-run found it; revoke-all removed it; final active count zero')


if __name__=='__main__':
    try:main()
    except Exception as error:
        reason={
            'Expected active nonprivileged issuer identity and unique login mapping':'identity',
            'Invalid management identity response':'users-response',
            'Invalid login mapping':'login-mapping',
            'Invalid budget response':'budgets-response',
            'Invalid budget row':'budget-row',
            'Invalid budget value':'budget-value',
            'Existing budget has no matching setup receipt; reconcile first':'existing-budget',
            'Access setup result uncertain; preserve private state and reconcile before retrying':'access-write',
        }.get(str(error),'other')
        for operation in ('budget','key-issuance','key-validation','expiry','key-storage'):
            if str(error)=='Access setup uncertain at '+operation+'; preserve private state and reconcile before retrying':reason=operation
        raise SystemExit('PROBE_FAILURE stage='+STAGE+' reason='+reason+' status='+str(STATUS)) from None
