#!/usr/bin/env python3
"""Verify issuer TLS, discovery and public admin rejection through Praxis."""
import argparse
import http.client
import ipaddress
import json
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit


def check(origin,ca):
    parsed=urlsplit(origin)
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.path not in ('','/')
            or parsed.query or parsed.fragment):
        raise ValueError('use the exact HTTPS issuer origin')
    ipaddress.IPv4Address(parsed.hostname)
    context=ssl.create_default_context(cafile=str(ca))
    def request(path):
        connection=http.client.HTTPSConnection(parsed.hostname,parsed.port or 443,timeout=5,context=context)
        try:
            connection.request('GET',path)
            response=connection.getresponse()
            return response.status,response.read(65537)
        finally:connection.close()
    discovery='/realms/enmaas-lab/.well-known/openid-configuration'
    for _ in range(60):
        try:
            status,body=request(discovery)
            if status==200:break
        except (OSError,http.client.HTTPException):pass
        time.sleep(1)
    else:raise RuntimeError('issuer discovery did not become ready')
    if len(body)>65536:raise RuntimeError('discovery response too large')
    document=json.loads(body)
    issuer=origin.rstrip('/')+'/realms/enmaas-lab'
    if document['issuer']!=issuer:raise RuntimeError('unexpected issuer')
    for key in ('authorization_endpoint','token_endpoint','jwks_uri'):
        if not document[key].startswith(issuer+'/'):raise RuntimeError('unexpected discovery endpoint')
    for path in ('/admin/','/admin/master/console/','/realms/master/.well-known/openid-configuration',
                 '/realms/enmaas-lab/../../admin/', '/realms/enmaas-lab/%2e%2e/%2e%2e/admin/',
                 '/realms/enmaas-lab/%252e%252e/%252e%252e/admin/',
                 '//admin/', '/resources/../admin/', '/resources/%2e%2e/admin/'):
        status,_=request(path)
        if status not in (400,403,404):raise RuntimeError(f'public boundary failed for {path}: HTTP {status}')
    print('PASS: verified issuer certificate and exact discovery; public admin/master and traversal probes denied')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--ca',type=Path,required=True)
    args=parser.parse_args()
    check(args.url,args.ca)
