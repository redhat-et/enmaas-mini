#!/usr/bin/env python3
"""Workstation-only private CA and least-privilege role bundles for the test lab."""
import argparse
import ipaddress
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess

ROLES=('gateway','keycloak','observability')


def dns_name(value):
    if not isinstance(value,str) or len(value)>253 or '.' not in value or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label)
            for label in value.split('.')):
        raise ValueError('use an explicit lowercase DNS name, without wildcard, URL or trailing dot')
    try:ipaddress.ip_address(value)
    except ValueError:return value
    raise ValueError('DNS identity must be a hostname, not an IP address')


def generate(output,hosts,days=30,*,dns_names=None):
    output=Path(output)
    if output.exists() or output.is_symlink():raise FileExistsError('choose a new output directory; existing keys are never replaced')
    if type(days) is not int or not 1<=days<=30:raise ValueError('test certificates must last 1–30 days')
    if set(hosts)!=set(ROLES):raise ValueError('provide exactly gateway, keycloak and observability addresses')
    clean={}
    for role in ROLES:
        if not isinstance(hosts[role],list) or not 1<=len(hosts[role])<=2:raise ValueError('each host needs a public IP and optional private IP')
        clean[role]=list(dict.fromkeys(str(ipaddress.IPv4Address(value)) for value in hosts[role]))
    if dns_names is None:dns_names={}
    if not isinstance(dns_names,dict) or not set(dns_names).issubset(ROLES):
        raise ValueError('DNS identities must refer only to the three host roles')
    names={role:dns_name(value) for role,value in dns_names.items()}
    oldmask=os.umask(0o077)
    try:
        output.mkdir(mode=0o700,parents=True)
        authorities=output/'authorities';authorities.mkdir(mode=0o700)
        trust=output/'trust';trust.mkdir(mode=0o700)
        def openssl(*args):
            subprocess.run(['openssl',*map(str,args)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for authority in ('issuer','telemetry'):
            openssl('req','-x509','-newkey','rsa:3072','-nodes','-days','90',
                '-subj',f'/CN=enMAAS test {authority} CA','-keyout',authorities/(authority+'-ca.key'),
                '-out',trust/(authority+'-ca.pem'),'-addext','basicConstraints=critical,CA:TRUE,pathlen:0',
                '-addext','keyUsage=critical,keyCertSign,cRLSign','-addext','subjectKeyIdentifier=hash')
        def leaf(directory,name,authority,purpose,addresses,hostname=None):
            csr=directory/(name+'.csr');extensions=directory/(name+'.ext')
            extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                +f'extendedKeyUsage={purpose}\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
                +'subjectAltName=DNS:localhost,'+('DNS:'+hostname+',' if hostname else '')
                +','.join('IP:'+ip for ip in dict.fromkeys([*addresses,'127.0.0.1']))+'\n')
            openssl('req','-new','-newkey','rsa:3072','-nodes','-subj',f'/CN=enMAAS test {directory.name} {name}',
                '-keyout',directory/(name+'.key'),'-out',csr)
            openssl('x509','-req','-in',csr,'-CA',trust/(authority+'-ca.pem'),'-CAkey',authorities/(authority+'-ca.key'),
                '-set_serial','0x'+secrets.token_hex(16),'-days',str(days),'-extfile',extensions,'-out',directory/(name+'.pem'))
            csr.unlink();extensions.unlink()
        for role in ROLES:
            bundle=output/role;bundle.mkdir(mode=0o700)
            leaf(bundle,'server','issuer','serverAuth',clean[role],names.get(role))
            shutil.copyfile(trust/'issuer-ca.pem',bundle/'ca.pem')
            shutil.copyfile(trust/'issuer-ca.pem',bundle/'issuer-ca.pem')
        leaf(output/'gateway','telemetry-client','telemetry','clientAuth',clean['gateway'])
        shutil.copyfile(trust/'issuer-ca.pem',output/'gateway/telemetry-server-ca.pem')
        shutil.copyfile(trust/'telemetry-ca.pem',output/'observability/telemetry-ca.pem')
        (output/'manifest.json').write_text(json.dumps({'test_only':True,'days':days,'hosts':clean,'dns_names':names,
            'authority_keys':'workstation only; never copy authorities to a VM',
            'bundles':list(ROLES)},indent=2)+'\n')
        for path in output.rglob('*'):path.chmod(0o700 if path.is_dir() else 0o600)
    finally:os.umask(oldmask)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for role in ROLES:parser.add_argument('--'+role,nargs='+',required=True,metavar='IP')
    for role in ROLES:parser.add_argument('--'+role+'-name',help='optional DNS SAN; does not configure DNS or issuer routing')
    parser.add_argument('--days',type=int,default=30)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    generate(args.output,{role:getattr(args,role) for role in ROLES},args.days,
             dns_names={role:getattr(args,role+'_name') for role in ROLES if getattr(args,role+'_name')})
    print('Created private role bundles:',args.output)
    print('Keep authorities/ on this workstation. Copy only each VM’s matching role directory.')

if __name__=='__main__':main()
