#!/usr/bin/env python3
"""Read one test user's password on the issuer host; never enumerate service secrets."""
import argparse
import json
import os
import re
from pathlib import Path
import stat
import sys
import uuid

ROOT=Path('/root/enmaas-keycloak-inputs')


def read(path):
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        parent=os.fstat(directory)
        if parent.st_uid!=0 or parent.st_mode&0o077:
            raise ValueError('Issuer input directory must be root-owned and private')
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    finally:os.close(directory)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077:
            raise ValueError('Issuer state must be a root-private regular file')
        raw=stream.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise ValueError('Issuer state exceeds the size limit')
    data=json.loads(raw)
    if not isinstance(data,dict):raise ValueError('Issuer state must be an object')
    return data


def password(root,user):
    if os.geteuid()!=0:raise ValueError('Run on the issuer host using sudo')
    identities=read(root/'identities.json')
    # Membership in the identity inventory prevents selecting db/client/bootstrap secrets.
    identity=identities.get(user)
    if not isinstance(identity,str) or str(uuid.UUID(identity))!=identity:
        raise ValueError('Unknown or invalid test identity')
    value=read(root/'credentials.json').get(user)
    if (not isinstance(value,str) or not 1<=len(value)<=1024
            or any(ord(c)<32 or ord(c)==127 for c in value)):
        raise ValueError('No valid saved password for this identity')
    return value


def population_password(root,index):
    if os.geteuid()!=0:raise ValueError('Run on the issuer host using sudo')
    manifest=read(root/'population.json')
    if (set(manifest)!={'version','prefix','count'} or type(manifest['version']) is not int
            or manifest['version']!=1 or type(manifest['count']) is not int
            or not 1<=manifest['count']<=10000 or not isinstance(manifest['prefix'],str)
            or not re.fullmatch(r'perf-[0-9a-f]{16}',manifest['prefix'])
            or type(index) is not int or not 1<=index<=manifest['count']):
        raise ValueError('Invalid population or selected user index')
    name=str(index).zfill(5)
    record=read(root/(name+'.json'))
    if (set(record)!={'username','password','state','user_id'} or record['state']!='created'
            or record['username']!=manifest['prefix']+'-'+name
            or not isinstance(record['user_id'],str) or str(uuid.UUID(record['user_id']))!=record['user_id']
            or not isinstance(record['password'],str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',record['password'])):
        raise ValueError('No completed matching identity with a valid saved password')
    return record['password']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    select=parser.add_mutually_exclusive_group(required=True)
    select.add_argument('--user',help='one bootstrap persona')
    select.add_argument('--index',type=int,help='one numbered synthetic population user')
    parser.add_argument('--inputs',type=Path,help='bootstrap-persona inputs directory')
    parser.add_argument('--population',type=Path,help='private population state on the issuer host')
    parser.add_argument('--stdout',action='store_true',help='explicitly emit only the selected password; pipe to a clipboard/password tool')
    args=parser.parse_args()
    if not args.stdout:raise ValueError('Explicit --stdout required; password is not printed by default')
    if args.index is not None:
        if args.population is None or args.inputs is not None:
            parser.error('--index requires --population and cannot use --inputs')
        value=population_password(args.population,args.index)
    else:
        if args.population is not None:parser.error('--population requires --index')
        value=password(args.inputs or ROOT,args.user)
    sys.stdout.write(value)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError):
        raise SystemExit('Password retrieval failed; check the selected test identity and root-private issuer files. No service credential is printed.') from None
