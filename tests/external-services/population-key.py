#!/usr/bin/env python3
"""Inspect or explicitly export one confirmed, currently active test inference key."""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('selected_key_population',HERE/'import-population.py')
population=importlib.util.module_from_spec(spec);spec.loader.exec_module(population)


def select(document,root,index,identity_read,key_list):
    population.validate(document)
    if type(index) is not int or not 1<=index<=len(document['profiles']):
        raise ValueError('Choose an existing population index')
    binding={'population':document['population'],
        'sha256':hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    if population.state.load(root,'import.json')!=binding:raise ValueError('Export and import state differ')
    profile=document['profiles'][index-1];identity=profile['user_id']
    if population.state.load(root,identity+'.json')!={'state':'applied','user_id':identity}:
        raise ValueError('Identity is not a confirmed active import')
    receipt=population.state.load(root,identity+'.access.json')
    if (not isinstance(receipt,dict) or receipt.get('state')!='configured' or receipt.get('user_id')!=identity
            or not isinstance(receipt.get('key_id'),str) or not receipt['key_id']):
        raise ValueError('Key setup is not confirmed')
    expiry=datetime.fromisoformat(receipt['expires_at'].replace('Z','+00:00'))
    if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError('Saved key expired')
    directory=population.state.secure_dir(root)
    try:fd=os.open(identity+'.key',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    finally:os.close(directory)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno());raw=source.read(4098)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
                or not 1<len(raw)<=4097 or not raw.endswith(b'\n')
                or any(c<33 or c>126 for c in raw[:-1])
                or hashlib.sha256(raw).hexdigest()!=receipt.get('key_sha256')):
            raise ValueError('Saved key is not a matching private regular file')
    current=identity_read(profile)
    if (not isinstance(current,dict) or current.get('user_id')!=identity or current.get('active') is not True):
        raise ValueError('Issuer identity is not currently active in PriceTag')
    keys=key_list(profile)
    if not isinstance(keys,list):raise ValueError('Invalid current key list')
    matches=[row for row in keys if isinstance(row,dict) and row.get('id')==receipt['key_id']]
    if (len(matches)!=1 or str(matches[0].get('status','')).lower()!='active'
            or matches[0].get('expirationDate')!=receipt['expires_at']):
        raise ValueError('Saved key is not uniquely active with its recorded expiry')
    return {'username':profile['tags']['email'].removesuffix('@example.test'),
            'login':profile['tags']['email'],'user_id':identity,'key_id':receipt['key_id'],
            'expires_at':receipt['expires_at'],'key_file':str(root/(identity+'.key'))},raw


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles',type=Path,default=Path('/root/enmaas-inputs/population-profiles.json'))
    parser.add_argument('--state',type=Path,default=Path('/root/enmaas-population-import'))
    parser.add_argument('--index',type=int,required=True)
    parser.add_argument('--stdout',action='store_true',help='explicitly emit only this key; redirect into a private file')
    args=parser.parse_args()
    if os.geteuid()!=0:raise ValueError('Run on the gateway host using sudo')
    result,raw=select(population.private.read(args.profiles),args.state,args.index,
        lambda profile:population.oidc.partner_request(profile,'GET'),population.oidc.list_keys)
    if args.stdout:sys.stdout.buffer.write(raw)
    else:print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        raise SystemExit('Key retrieval failed; check the selected identity, key status and private receipts. No key printed.') from None
