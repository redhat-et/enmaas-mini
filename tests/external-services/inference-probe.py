#!/usr/bin/env python3
"""Run inside the isolated pod; credentials never leave this process."""
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error
import uuid
sys.path.insert(0,'/fixture')
from transport import MockTransport

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def main():
    token=Path('/fixture/management').read_text().strip()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def api(route,body,method='PUT'):
        req=urllib.request.Request('http://127.0.0.1:8087/api/v1/management/'+route,
            data=json.dumps(body).encode(),method=method,
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with opener.open(req,timeout=15) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            print(f'Private management {route}: HTTP {error.code}',file=sys.stderr)
            raise
    subject='lab-probe-'+uuid.uuid4().hex
    created=False
    try:
        api('users',{'subject':subject,'name':'Synthetic inference probe','role':'user','active':True})
        created=True
        api('budgets',{'subject':subject,'monthly_usd':5})
        api('prices',{'model':'fixture','provider':'vllm','input_cost_per_mtok':1,'output_cost_per_mtok':2,'cache_write_cost_per_mtok':0,'cache_read_cost_per_mtok':0})
        key=api('keys',{'subject':subject,'name':'fixture','expiresIn':'1h'},'POST')['key']
        for stream in (False,True):
            with MockTransport('https://localhost:28443',Path('/fixture/ca.pem'),[key],'fixture',stream=stream) as request:
                result=request(0,15)
            if result['status']!=200:raise ValueError('Mock inference denied')
        print(json.dumps({'passed':True,'subject':subject,'completed':2,'input_tokens':4,'output_tokens':6}))
    finally:
        if created:
            try:api('key-revocations',{'subject':subject},'POST')
            finally:api('users',{'subject':subject,'active':False})

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Failure class: '+type(error).__name__,file=sys.stderr)
        raise SystemExit('Inference probe failed; inspect private fixture services; no credential diagnostics printed') from None
