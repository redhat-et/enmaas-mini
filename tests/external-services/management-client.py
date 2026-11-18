#!/usr/bin/env python3
"""Private fixture API bridge; stdin/stdout are captured by the local runner."""
import json
from pathlib import Path
import sys
import uuid
import urllib.request
import urllib.error

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def main():
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536:raise ValueError('input too large')
    print(json.dumps(execute(json.loads(raw))))

def serve():
    # Private pipe protocol only; no new listener or exposed credentials.
    for _ in range(30000):
        raw=sys.stdin.buffer.readline(65537)
        if not raw:return
        if len(raw)>65536 or not raw.endswith(b'\n'):raise ValueError('invalid command frame')
        print(json.dumps(execute(json.loads(raw))),flush=True)
    raise ValueError('operation bound exceeded')

def execute(command):
    route=command['route'];method=command['method']
    if (method,route) == ('PATCH','profile'):
        body=command['body']
        identity=body['user_id']
        if str(uuid.UUID(identity)) != identity or set(body) != {'user_id','tags'}:
            raise ValueError('invalid synthetic profile')
        if set(body['tags']) - {'email','first_name','last_name','country','state','department','cost_center','cost_center_description','employee_type'}:
            raise ValueError('profile patch may only set synthetic display metadata')
        email=body['tags'].get('email','')
        if not __import__('re').fullmatch(r'lab-run-[a-f0-9]{32}-[0-9]{1,4}@example\.test',email):
            raise ValueError('Synthetic profile requires isolated test email')
        if int(email.rsplit('-',1)[1].split('@')[0])>=2500:raise ValueError('Synthetic population bound exceeded')
        token=Path('/fixture/partner').read_text().strip()
        url='http://127.0.0.1:8087/api/v1/users/'+identity
        payload={'tags':body['tags']}
    elif (method,route) not in {('PUT','users'),('PUT','budgets'),('PUT','prices'),('POST','keys'),('POST','key-revocations')}:
        raise ValueError('unsupported fixture operation')
    else:
        token=Path('/fixture/management').read_text().strip()
        url='http://127.0.0.1:8087/api/v1/management/'+route
        payload=command['body']
    request=urllib.request.Request(url,
        data=json.dumps(payload).encode(),method=method,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=15) as response:
        data=response.read(1048577)
        if len(data)>1048576:raise ValueError('response too large')
        result=json.loads(data)
    return result

if __name__=='__main__':
    try:
        if sys.argv[1:]==['--serve']:serve()
        elif not sys.argv[1:]:main()
        else:raise ValueError('unsupported argument')
    except urllib.error.HTTPError as error:raise SystemExit('Private fixture management HTTP '+str(error.code)) from None
    except Exception:raise SystemExit('Private fixture management operation failed') from None
