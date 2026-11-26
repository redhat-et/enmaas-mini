#!/usr/bin/env python3
"""Preview or add nonsecret scoped-role settings to an existing private AWS config.

No AWS calls, access-key creation, credential-file edits or automatic role assumption.
"""
import argparse
import configparser
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import secrets


def settings(account,region,group,source,profile=None,mfa=None,role_source=False):
    profile=profile or 'enmaas-'+group
    if (not re.fullmatch(r'[0-9]{12}',account) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]',region)
            or not re.fullmatch(r'[a-z][a-z0-9]{0,7}',group)):
        raise ValueError('Invalid account, region or deployment group')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value) for value in (source,profile)):
        raise ValueError('Use explicit profile names')
    if profile==source or profile=='default':raise ValueError('Use a distinct named target profile')
    if role_source:
        if mfa is not None:raise ValueError('Role-source authentication is reviewed at its source; do not assert user MFA')
    elif (not isinstance(mfa,str) or not re.fullmatch(r'arn:aws:iam::'+account+r':mfa/[A-Za-z0-9_+=,.@/-]+|[A-Z0-9]{6,256}',mfa)):
        raise ValueError('IAM-user source requires its MFA device ARN or hardware serial')
    values={'role_arn':'arn:aws:iam::'+account+':role/enmaas/em-'+group+'-operator',
            'source_profile':source,'region':region,'duration_seconds':'3600',
            'role_session_name':'enmaas-'+group,'output':'json'}
    if mfa is not None:values['mfa_serial']=mfa
    return profile,values


def read_config(directory,name):
    try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    except FileNotFoundError:return None
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('AWS config must be a private user-owned regular file')
        raw=source.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('AWS config exceeds limit')
    return raw


def append_profile(path,profile,values):
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    lock=None;temporary=None
    try:
        info=os.fstat(directory)
        if info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('AWS config directory must be private and user-owned')
        lock=os.open('.enmaas-profile.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=read_config(directory,path.name)
        parser=configparser.RawConfigParser(strict=True)
        parser.read_string((before or b'').decode('utf-8'))
        section='profile '+profile
        if parser.has_section(section):raise ValueError('Target profile already exists; no profile overwritten')
        addition='\n['+section+']\n'+''.join(key+' = '+value+'\n' for key,value in values.items())
        temporary='.enmaas-profile-'+secrets.token_hex(12)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as output:
            output.write((before or b'')+addition.encode());output.flush();os.fsync(output.fileno())
        if read_config(directory,path.name)!=before:raise ValueError('AWS config changed during preparation')
        if before is None:os.link(temporary,path.name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
        else:os.replace(temporary,path.name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if temporary is not None:
            try:os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError:pass
        if lock is not None:os.close(lock)
        os.close(directory)
    return {'profile':profile,'config_file':str(path),'credentials_written':False,'role_assumed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--region',default='eu-central-1')
    parser.add_argument('--group',required=True)
    parser.add_argument('--source-profile',required=True)
    parser.add_argument('--profile')
    parser.add_argument('--mfa-serial')
    parser.add_argument('--role-source',action='store_true',help='only for a source role explicitly approved in the operator trust policy')
    parser.add_argument('--config-file',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    profile,values=settings(args.account,args.region,args.group,args.source_profile,args.profile,args.mfa_serial,args.role_source)
    if args.apply:
        if args.config_file is None or not args.config_file.is_absolute():raise ValueError('Apply requires an explicit absolute --config-file')
        result=append_profile(args.config_file,profile,values)
    else:result={'profile':profile,'settings':values,'changed':False}
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,configparser.Error):
        raise SystemExit('Operator profile setup stopped; check inputs and private config permissions. Existing profiles and credential values are not printed.') from None
