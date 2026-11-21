#!/usr/bin/env python3
"""Transfer a reviewed issuer file over verified SSH without printing its contents."""
import argparse
import ipaddress
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('transfer_population',HERE/'import-population.py')
population=importlib.util.module_from_spec(spec);spec.loader.exec_module(population)

spec=importlib.util.spec_from_file_location('transfer_personas',HERE/'personas.py')
personas=importlib.util.module_from_spec(spec);spec.loader.exec_module(personas)

FILES={
    'personas':('/root/enmaas-keycloak-inputs/identities.json','/root/enmaas-inputs/personas.json'),
    'population':('/root/enmaas-test-users/profiles.json','/root/enmaas-inputs/population-profiles.json'),
    'gateway':('/root/enmaas-keycloak-inputs/gateway-oidc.json','/root/enmaas-inputs/oidc.json'),
    'observability':('/root/enmaas-keycloak-inputs/observability-client-secret','/root/enmaas-inputs/observability-client-secret'),
}
BUNDLES={
    'keycloak':('server.pem','server.key','ca.pem','issuer-ca.pem'),
    'gateway':('server.pem','server.key','ca.pem','issuer-ca.pem',
               'telemetry-client.pem','telemetry-client.key','telemetry-server-ca.pem'),
    'observability':('server.pem','server.key','ca.pem','issuer-ca.pem','telemetry-ca.pem'),
}
# The remote destination is created exclusively, after validating all input.
# A failed write leaves the partial directory for inspection; never overwrite it.
BUNDLE_WRITE='''import json,os,stat,sys
p=sys.argv[1];names=json.loads(sys.argv[2]);parent=os.path.dirname(p)
s=os.lstat(parent)
if os.geteuid()!=0 or not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077:sys.exit(2)
raw=sys.stdin.buffer.read(524289)
if len(raw)>524288:sys.exit(2)
data=json.loads(raw)
if not isinstance(data,dict) or set(data)!=set(names):sys.exit(2)
for name,value in data.items():
 if not isinstance(value,str) or not 0<len(value.encode('ascii'))<=65536:sys.exit(2)
os.mkdir(p,0o700)
for name in names:
 fd=os.open(os.path.join(p,name),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as f:
  f.write(data[name].encode('ascii'));f.flush();os.fsync(f.fileno())
for directory in (p,parent):
 fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
'''

# Run on the selected host as root. Only reviewed fixed paths enter these scripts.
READ='''import os,stat,sys
p=sys.argv[1]
fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
with os.fdopen(fd,'rb') as f:
 s=os.fstat(f.fileno())
 if os.geteuid()!=0 or not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077 or not 0<s.st_size<=65536:sys.exit(2)
 data=f.read(65537)
 if not 0<len(data)<=65536:sys.exit(2)
 sys.stdout.buffer.write(data)
'''
WRITE='''import os,stat,sys,tempfile
p=sys.argv[1];parent=os.path.dirname(p)
s=os.lstat(parent)
if os.geteuid()!=0 or not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077:sys.exit(2)
data=sys.stdin.buffer.read(65537)
if not 0<len(data)<=65536:sys.exit(2)
fd,tmp=tempfile.mkstemp(prefix='.client-',dir=parent)
try:
 with os.fdopen(fd,'wb') as f:
  f.write(data);f.flush();os.fsync(f.fileno())
 os.link(tmp,p,follow_symlinks=False)
 d=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(d)
 finally:os.close(d)
finally:os.unlink(tmp)
'''


def target(value):
    match=re.fullmatch(r'([a-z_][a-z0-9_-]{0,31})@(.+)',value)
    if not match:raise ValueError('Use an explicit SSH user@IPv4 address')
    return match[1]+'@'+str(ipaddress.IPv4Address(match[2]))


def private_file(path):
    path=Path(path)
    if '%' in str(path) or any(ord(c)<32 for c in str(path)):
        raise ValueError('SSH paths must not contain expansion tokens or control characters')
    s=path.lstat()
    if not path.is_absolute() or not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_mode&0o077:
        raise ValueError('SSH key and verified known-hosts file must be private, user-owned regular files at absolute paths')
    return str(path)


def ssh_options(key,known_hosts):
    known=private_file(known_hosts).replace('\\','\\\\').replace('"','\\"')
    return ['ssh','-F','/dev/null','-i',private_file(key),'-o','IdentitiesOnly=yes',
        '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile="'+known+'"',
        '-o','GlobalKnownHostsFile=/dev/null','-o','UpdateHostKeys=no','-o','ConnectTimeout=10',
        '-o','ForwardAgent=no','-o','ClearAllForwardings=yes','-o','RequestTTY=no']

def transfer(source,destination,kind,key,known_hosts):
    source,destination=target(source),target(destination)
    if source.split('@')[1]==destination.split('@')[1]:raise ValueError('Issuer and consuming service must use separate hosts')
    if kind not in FILES:raise ValueError('Unknown client kind')
    options=ssh_options(key,known_hosts)
    source_path,dest_path=FILES[kind]
    limit=8*1024*1024 if kind=='population' else 65536
    def run(host,script,path,**kwargs):
        script=script.replace('65537',str(limit+1)).replace('65536',str(limit))
        command=shlex.join(['sudo','-n','python3','-c',script,path])
        try:
            result=subprocess.run([*options,host,command],capture_output=True,timeout=45,**kwargs)
            if result.returncode:raise ValueError('SSH operation failed')
            return result.stdout
        except (OSError,ValueError,subprocess.TimeoutExpired):
            raise ValueError('Client transfer failed; verify host trust, source permissions and destination state. No secret output is displayed; do not overwrite an existing destination.') from None
    data=run(source,READ,source_path,stdin=subprocess.DEVNULL)
    if not 0<len(data)<=limit:raise ValueError('Client source is empty or oversized; destination unchanged')
    if kind=='population':population.validate(json.loads(data))
    if kind=='personas':personas.validate(json.loads(data))
    run(destination,WRITE,dest_path,input=data)
    return {'client':kind,'destination':destination,'path':dest_path}


def transfer_bundle(destination,role,bundle,key,known_hosts):
    destination=target(destination)
    if role not in BUNDLES:raise ValueError('Choose a service role, never an authority bundle')
    options=ssh_options(key,known_hosts)
    directory=population.state.secure_dir(bundle)
    payload={}
    try:
        for name in BUNDLES[role]:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                    raise ValueError('Bundle files must be private user-owned regular files')
                raw=stream.read(65537)
            if not 0<len(raw)<=65536:raise ValueError('Empty or oversized bundle file')
            payload[name]=raw.decode('ascii')
    finally:os.close(directory)
    command=shlex.join(['sudo','-n','python3','-c',BUNDLE_WRITE,'/root/enmaas-tls',json.dumps(BUNDLES[role])])
    try:
        result=subprocess.run([*options,destination,command],input=json.dumps(payload).encode(),capture_output=True,timeout=45)
        if result.returncode:raise ValueError('Transfer failed')
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise ValueError('Bundle transfer failed; inspect the selected host for partial state before retrying. No private values printed.') from None
    return {'client':role+' TLS','destination':destination,'path':'/root/enmaas-tls'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issuer')
    parser.add_argument('--destination',required=True)
    parser.add_argument('--client',choices=FILES)
    parser.add_argument('--bundle',type=Path,help='local role directory, never the parent certificate directory')
    parser.add_argument('--role',choices=BUNDLES)
    parser.add_argument('--ssh-key',type=Path,required=True)
    parser.add_argument('--known-hosts',type=Path,required=True)
    args=parser.parse_args()
    if args.bundle is not None or args.role is not None:
        if args.bundle is None or args.role is None or args.issuer or args.client:
            raise ValueError('Bundle mode requires only --bundle and --role, not issuer/client')
        result=transfer_bundle(args.destination,args.role,args.bundle,args.ssh_key,args.known_hosts)
    else:
        if not args.issuer or not args.client:raise ValueError('Supply issuer and client, or bundle and role')
        result=transfer(args.issuer,args.destination,args.client,args.ssh_key,args.known_hosts)
    print('Installed '+result['client']+' client file on '+result['destination']+': '+result['path'])


if __name__=='__main__':
    try:main()
    except (OSError,ValueError):raise SystemExit('Client transfer stopped; verify protected files and SSH configuration. No credential values printed.') from None
