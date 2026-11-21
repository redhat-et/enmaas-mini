"""Private, resumable issuer-user records; HTTP transport and PriceTag mapping are separate.

A lost creation response leaves an explicit uncertain record. Never adopt a user
by username/email or automatically repeat a creation whose outcome is unknown.
"""
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import uuid


def secure_dir(root):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    info=os.fstat(fd)
    if info.st_uid!=os.getuid() or info.st_mode&0o077:
        os.close(fd);raise ValueError('Population directory must be owned by this user and private')
    return fd


def save(root,name,value,exclusive=False):
    raw=(json.dumps(value,sort_keys=True)+'\n').encode()
    fd,tmp=tempfile.mkstemp(prefix='.population-',dir=root)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if exclusive:os.link(tmp,root/name,follow_symlinks=False)
        else:os.replace(tmp,root/name)
        directory=secure_dir(root)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:Path(tmp).unlink(missing_ok=True)


def load(root,name):
    directory=secure_dir(root)
    try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    finally:os.close(directory)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('Population record must be private and user-owned')
        raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Population record is oversized')
    return json.loads(raw)


def initialize(root,count):
    if type(count) is not int or not 1<=count<=10000:raise ValueError('Choose 1–10000 synthetic users')
    root.mkdir(mode=0o700)
    directory=secure_dir(root);os.close(directory)
    manifest={'version':1,'prefix':'perf-'+secrets.token_hex(8),'count':count}
    save(root,'population.json',manifest,exclusive=True)
    return manifest


def manifest(root):
    manifest=load(root,'population.json')
    if (not isinstance(manifest,dict) or set(manifest)!={'version','prefix','count'}
            or manifest['version']!=1 or type(manifest['count']) is not int
            or not 1<=manifest['count']<=10000):raise ValueError('Invalid population manifest')
    prefix=manifest['prefix']
    if not isinstance(prefix,str) or len(prefix)!=21 or not prefix.startswith('perf-'):
        raise ValueError('Invalid population prefix')
    if any(c not in '0123456789abcdef' for c in prefix[5:]):raise ValueError('Invalid population prefix')
    return manifest


def run(root,create_user,inspect_user):
    """Callbacks must return the created canonical UUID and a verified user object.

    Errors intentionally omit callback diagnostics, which may contain credentials.
    This creates issuer identities only; it does not configure roles or PriceTag.
    """
    directory=secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        settings=manifest(root);prefix=settings['prefix']
        for index in range(1,settings['count']+1):
            name=str(index).zfill(5)+'.json';username=prefix+'-'+str(index).zfill(5)
            try:record=load(root,name)
            except FileNotFoundError:
                record={'username':username,'password':secrets.token_urlsafe(32),'state':'prepared','user_id':None}
                save(root,name,record,exclusive=True)
            if (not isinstance(record,dict) or set(record)!={'username','password','state','user_id'}
                    or record['username']!=username or not isinstance(record['password'],str)
                    or not 32<=len(record['password'])<=128
                    or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in record['password'])):
                raise ValueError('Invalid population user record')
            if record['state']=='creating':raise ValueError('Creation outcome uncertain; reconcile the private record before continuing')
            if record['state']=='prepared':
                if record['user_id'] is not None:raise ValueError('Prepared user already has an identity')
                record['state']='creating';save(root,name,record)
                profile={'username':username,'email':username+'@example.test','enabled':True,'emailVerified':True,
                    'firstName':'Synthetic','lastName':'User','groups':[],
                    'credentials':[{'type':'password','value':record['password'],'temporary':False}]}
                try:
                    identity=create_user(profile)
                    if not isinstance(identity,str) or str(uuid.UUID(identity))!=identity:raise ValueError('Invalid identity')
                except Exception:raise ValueError('User creation not confirmed; preserve the private record for reconciliation') from None
                record.update(state='created',user_id=identity);save(root,name,record)
            if record['state']!='created':raise ValueError('Unknown population user state')
            identity=record['user_id']
            if not isinstance(identity,str) or str(uuid.UUID(identity))!=identity:raise ValueError('Invalid recorded identity')
            try:
                actual=inspect_user(identity)
                if actual.get('id')!=identity or actual.get('username')!=username or actual.get('enabled') is not True:
                    raise ValueError('Identity mismatch')
            except Exception:raise ValueError('Recorded issuer identity did not verify; no replacement was created') from None
        return {'issuer_users_verified':settings['count'],'pricetag_provisioned':False}
    finally:os.close(lock)


def export_profiles(root):
    """Whitelist only public identity attributes; never export credential records."""
    directory=secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        settings=manifest(root);profiles=[];seen=set()
        for index in range(1,settings['count']+1):
            record=load(root,str(index).zfill(5)+'.json')
            username=settings['prefix']+'-'+str(index).zfill(5)
            if not isinstance(record,dict) or record.get('state')!='created' or record.get('username')!=username:
                raise ValueError('Export requires completed, matching issuer records')
            identity=record.get('user_id')
            if not isinstance(identity,str) or str(uuid.UUID(identity))!=identity or identity in seen:
                raise ValueError('Export requires unique canonical issuer UUIDs')
            seen.add(identity)
            profiles.append({'user_id':identity,'tags':{'email':username+'@example.test','first_name':'Synthetic','last_name':'User'}})
        return {'version':1,'population':settings['prefix'],'profiles':profiles}
    finally:os.close(lock)
