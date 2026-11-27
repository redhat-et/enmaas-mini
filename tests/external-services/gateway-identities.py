#!/usr/bin/env python3
"""Fixture-only partner API checks, executed inside the isolated lab pod."""
import json
import sys
from pathlib import Path
import urllib.request
import urllib.error

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def main():
    root=Path('/fixture')
    identities=json.loads((root/'identities.json').read_text())
    secret=(root/'partner').read_text().strip()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    if len(sys.argv)>1 and sys.argv[1]=='deactivate':
        request=urllib.request.Request('http://127.0.0.1:8087/api/v1/users/'+identities['alice'],
            method='DELETE',headers={'Authorization':'Bearer '+secret})
        with opener.open(request,timeout=10) as response:result=json.load(response)
        if result.get('active') is not False or result.get('keys_revoked')!=0:
            raise RuntimeError('Deactivation contract differs')
        print('PASS: synthetic identity disabled through partner API')
        return
    for name in ('admin','alice'):
        url='http://127.0.0.1:8087/api/v1/users/'+identities[name]
        body=json.dumps({'tags':{'email':name+'@example.test','first_name':name.title(),'last_name':'Lab'}}).encode()
        unauthorized=urllib.request.Request(url,data=body,method='PUT',headers={'Content-Type':'application/json'})
        try:opener.open(unauthorized,timeout=10)
        except urllib.error.HTTPError as error:
            if error.code!=401:raise RuntimeError('Unexpected anonymous partner API status') from None
        else:raise RuntimeError('Anonymous partner write accepted')
        request=urllib.request.Request(url,data=body,method='PUT',headers={
            'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
        with opener.open(request,timeout=10) as response:
            result=json.load(response)
        if result.get('user_id')!=identities[name] or result.get('active') is not True:
            raise RuntimeError('Partner identity contract differs')
    print('PASS: anonymous partner writes denied; two immutable identities provisioned through API')

if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Partner fixture check failed; no credential diagnostics printed') from None
