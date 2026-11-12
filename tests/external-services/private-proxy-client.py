#!/usr/bin/env python3
"""Exercise the pinned OAuth2 Proxy through private DNS and real issuer login."""
import http.cookiejar
import json
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0,'/run/secrets')
from issuer_client import Form, document

ISSUER='https://identity.lab.example.com:8443'
PROXY='https://observability.lab.example.com:19443'


class LabRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not any(url.startswith(origin+'/') for origin in (ISSUER,PROXY)):
            raise RuntimeError('redirect outside isolated fixture')
        return super().redirect_request(request,response,code,message,headers,url)


def client():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile='/run/secrets/browser-ca.pem')),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),LabRedirect())


def main():
    credentials=json.loads(Path('/run/secrets/credentials.json').read_text())
    for headers in ({},{'X-Auth-Request-Groups':'/observability-admin','X-Forwarded-User':'admin'}):
        try:
            document(client(),urllib.request.Request(PROXY+'/oauth2/auth',headers=headers))
        except urllib.error.HTTPError as error:
            if error.code!=401:raise RuntimeError('unexpected anonymous authorization result') from None
            error.close()
        else:raise RuntimeError('anonymous or forged-header authorization accepted')
    for name in ('admin','alice','bob'):
        opener=client()
        form=Form();form.feed(document(opener,PROXY+'/').decode())
        if not form.action or not form.action.startswith(ISSUER+'/'):
            raise RuntimeError('expected issuer login form')
        request=urllib.request.Request(form.action,data=urllib.parse.urlencode(
            {'username':name,'password':credentials[name]}).encode())
        try:
            with opener.open(request,timeout=10) as response:
                if name!='admin' or response.status!=202 or not response.url.startswith(PROXY+'/'):
                    raise RuntimeError('unexpected protected upstream access')
            with opener.open(PROXY+'/oauth2/auth',timeout=10) as response:
                if response.status!=202:raise RuntimeError('administrator session not accepted')
        except urllib.error.HTTPError as error:
            if name=='admin' or error.code!=403:
                raise RuntimeError('unexpected group authorization result') from None
            error.close()
        print('PASS: private OAuth2 Proxy '+name+(' authenticated session accepted' if name=='admin' else ' denied by administrator-group gate'))
    print('PASS: anonymous and forged identity headers cannot authorize the private proxy')


if __name__=='__main__':
    try:main()
    except Exception:
        raise SystemExit('Private proxy check failed; credentials and sensitive responses withheld') from None
