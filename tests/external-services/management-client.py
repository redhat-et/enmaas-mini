#!/usr/bin/env python3
"""Private fixture API bridge; stdin/stdout are captured by the local runner."""
import json
from pathlib import Path
import sys
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def main():
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536:raise ValueError('input too large')
    command=json.loads(raw)
    route=command['route'];method=command['method']
    if (method,route) not in {('PUT','users'),('PUT','budgets'),('PUT','prices'),('POST','keys'),('POST','key-revocations')}:
        raise ValueError('unsupported fixture operation')
    token=Path('/fixture/management').read_text().strip()
    request=urllib.request.Request('http://127.0.0.1:8087/api/v1/management/'+route,
        data=json.dumps(command['body']).encode(),method=method,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=15) as response:
        data=response.read(1048577)
        if len(data)>1048576:raise ValueError('response too large')
        result=json.loads(data)
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Private fixture management operation failed') from None
