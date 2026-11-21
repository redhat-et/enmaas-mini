#!/usr/bin/env python3
"""Import credential-free issuer profiles through PriceTag's private partner API."""
import argparse
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import math
import stat
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('import_population_state',HERE/'user-population.py')
state=importlib.util.module_from_spec(spec);spec.loader.exec_module(state)
spec=importlib.util.spec_from_file_location('import_private_profile',HERE/'keycloak-password.py')
private=importlib.util.module_from_spec(spec);spec.loader.exec_module(private)
sys.path.insert(0,str(HERE.parents[1]/'scripts/pricetag'))
loader=importlib.machinery.SourceFileLoader('population_oidc_user',str(HERE.parents[1]/'scripts/pricetag/oidc-user'))
spec=importlib.util.spec_from_loader(loader.name,loader)
oidc=importlib.util.module_from_spec(spec);loader.exec_module(oidc)
import metering_admin


def validate(document):
    if (not isinstance(document,dict) or set(document)!={'version','population','profiles'}
            or document['version']!=1 or not isinstance(document['population'],str)
            or not re.fullmatch(r'perf-[0-9a-f]{16}',document['population'])
            or not isinstance(document['profiles'],list) or not 1<=len(document['profiles'])<=10000):
        raise ValueError('Invalid population export')
    seen=set()
    for index,profile in enumerate(document['profiles'],1):
        oidc.validate(profile)
        if profile['user_id'] in seen or profile['tags']['email']!=document['population']+'-'+str(index).zfill(5)+'@example.test':
            raise ValueError('Duplicate identity or profile outside the selected population')
        seen.add(profile['user_id'])
    return document


def apply(document,root,provision,*,retire=False):
    validate(document)
    binding={'population':document['population'],
        'sha256':hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    if retire and not root.exists():raise ValueError('Retirement requires the confirmed import state')
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
        state.save(root,'import.json',binding,exclusive=True)
    directory=state.secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if state.load(root,'import.json')!=binding:raise ValueError('Import state is bound to a different export')
        for profile in document['profiles']:
            identity=profile['user_id'];name=identity+'.json'
            try:receipt=state.load(root,name)
            except FileNotFoundError:receipt=None
            if retire:
                if receipt=={'state':'retired','user_id':identity}:continue
                if receipt!={'state':'applied','user_id':identity}:
                    raise ValueError('Retirement requires confirmed imports; reconcile uncertain records')
            elif receipt is not None:
                if receipt!={'state':'applied','user_id':identity}:
                    raise ValueError('Uncertain, retired or invalid import record; reconcile before continuing')
                continue
            state.save(root,name,{'state':'retiring' if retire else 'applying','user_id':identity},exclusive=not retire)
            try:
                result=provision(profile)
                if not isinstance(result,dict) or result.get('user_id')!=identity:raise ValueError('Identity mismatch')
                if retire and (result.get('active') is not False or type(result.get('keys_revoked')) is not int or result['keys_revoked']<0):
                    raise ValueError('Deactivation/key revocation not confirmed')
            except Exception:
                raise ValueError('Partner API result uncertain; preserve import state for reconciliation') from None
            state.save(root,name,{'state':'retired' if retire else 'applied','user_id':identity})
        if retire:
            return {'recorded_retirements':len(document['profiles']),'issuer_accounts_removed':False}
        return {'recorded_imports':len(document['profiles']),'roles_configured':False,'budgets_configured':False}
    finally:os.close(lock)


def clear_keys(document,root,management,*,execute=False):
    """Revoke all key paths only for confirmed retired population identities."""
    validate(document)
    binding={'population':document['population'],
        'sha256':hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    directory=state.secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if state.load(root,'import.json')!=binding:raise ValueError('Import state is bound to a different export')
        for profile in document['profiles']:
            identity=profile['user_id']
            if state.load(root,identity+'.json')!={'state':'retired','user_id':identity}:
                raise ValueError('All-key cleanup requires confirmed retired identities')
        users=management('GET','users')
        if not isinstance(users,list):raise ValueError('Invalid management identity response')
        for profile in document['profiles']:
            identity=profile['user_id'];login=profile['tags']['email']
            matches=[row for row in users if isinstance(row,dict) and row.get('user_id')==identity]
            owners=[row for row in users if isinstance(row,dict) and isinstance(row.get('logins'),list)
                    and login in row['logins']]
            if (len(matches)!=1 or matches[0].get('active') is not False
                    or len(owners)!=1 or owners[0]!=matches[0]):
                raise ValueError('Inactive identity and login ownership must match the import')
        total=0;changed=False
        for profile in document['profiles']:
            identity=profile['user_id'];name=identity+'.keys.json'
            try:receipt=state.load(root,name)
            except FileNotFoundError:receipt=None
            if receipt is not None and receipt!={'state':'cleared','user_id':identity}:
                raise ValueError('Uncertain key cleanup; reconcile before continuing')
            mutate=execute and receipt is None
            if mutate:state.save(root,name,{'state':'clearing','user_id':identity},exclusive=True)
            try:
                def request(dry_run):
                    result=management('POST','key-revocations',{'subject':profile['tags']['email'],'dry_run':dry_run})
                    if (not isinstance(result,dict) or type(result.get('count')) is not int
                            or result['count']<0 or result.get('dry_run') is not dry_run):
                        raise ValueError('Invalid revocation result')
                    return result['count']
                if mutate:request(False)
                count=request(True)
                if (mutate or receipt is not None) and count!=0:
                    raise ValueError('Active keys remain after cleanup')
            except Exception:
                raise ValueError('Key cleanup result uncertain; preserve private state for reconciliation') from None
            if mutate:
                state.save(root,name,{'state':'cleared','user_id':identity})
                changed=True
            total+=count
        return {'users_checked':len(document['profiles']),'active_keys':total,'changed':changed}
    finally:os.close(lock)


def configure_access(document,root,management,*,monthly_usd=5,key_lifetime='24h',list_keys=None):
    """Configure only confirmed synthetic user identities; never grant a role."""
    validate(document)
    if (type(monthly_usd) not in (int,float) or not math.isfinite(monthly_usd) or not .01<=monthly_usd<=10000
            or not math.isclose(monthly_usd*100,round(monthly_usd*100),abs_tol=1e-8,rel_tol=0)):
        raise ValueError('Budget must be 0.01–10000 USD in whole cents')
    if key_lifetime not in ('1h','24h'):raise ValueError('Use a finite test-key lifetime of 1h or 24h')
    binding={'population':document['population'],
        'sha256':hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    directory=state.secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if state.load(root,'import.json')!=binding:raise ValueError('Import state is bound to another export')
        for profile in document['profiles']:
            identity=profile['user_id']
            if state.load(root,identity+'.json')!={'state':'applied','user_id':identity}:
                raise ValueError('Access setup requires confirmed active imports')
        users=management('GET','users')
        if not isinstance(users,list):raise ValueError('Invalid management identity response')
        by_id={};by_login={}
        for user in users:
            if not isinstance(user,dict) or not isinstance(user.get('logins'),list):raise ValueError('Invalid login mapping')
            by_id.setdefault(user.get('user_id'),[]).append(user)
            for login in user['logins']:by_login.setdefault(login,[]).append(user.get('user_id'))
        for profile in document['profiles']:
            identity=profile['user_id'];login=profile['tags']['email'];matches=by_id.get(identity,[])
            if (len(matches)!=1 or matches[0].get('active') is not True or matches[0].get('role')!='user'
                    or by_login.get(login)!=[identity]):
                raise ValueError('Expected active nonprivileged issuer identity and unique login mapping')
        budgets=management('GET','budgets')
        if not isinstance(budgets,list):raise ValueError('Invalid budget response')
        indexed={}
        for row in budgets:
            if not isinstance(row,dict):raise ValueError('Invalid budget row')
            if row.get('scope')=='user':
                amount=row.get('monthly_usd')
                if type(amount) not in (int,float) or not math.isfinite(amount):raise ValueError('Invalid budget value')
                indexed.setdefault(row.get('principal'),[]).append(amount)
        def expiry(value):
            if not isinstance(value,str):raise ValueError('Finite expiry required')
            deadline=datetime.fromisoformat(value.replace('Z','+00:00'))
            if deadline.tzinfo is None:raise ValueError('Expiry timezone required')
            remaining=(deadline-datetime.now(timezone.utc)).total_seconds()
            if not 0<remaining<=({'1h':3600,'24h':86400}[key_lifetime]+120):raise ValueError('Key expired or exceeds requested lifetime')
        for profile in document['profiles']:
            identity=profile['user_id'];login=profile['tags']['email'];name=identity+'.access.json'
            expected={'state':'configured','user_id':identity,'monthly_usd':monthly_usd,'key_lifetime':key_lifetime}
            try:receipt=state.load(root,name)
            except FileNotFoundError:receipt=None
            if receipt is not None:
                if (not isinstance(receipt,dict) or set(receipt)!=set(expected)|{'key_id','expires_at','key_sha256'}
                        or any(receipt.get(k)!=v for k,v in expected.items()) or indexed.get(login)!=[monthly_usd]):
                    raise ValueError('Access state uncertain or changed; reconcile without issuing another key')
                expiry(receipt['expires_at'])
                fd=os.open(root/(identity+'.key'),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as source:
                    info=os.fstat(source.fileno());raw=source.read(4098)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
                            or not 1<len(raw)<=4097 or not raw.endswith(b'\n')
                            or hashlib.sha256(raw).hexdigest()!=receipt['key_sha256']):
                        raise ValueError('Saved key must remain a private regular file')
                continue
            if login in indexed:raise ValueError('Existing budget has no matching setup receipt; reconcile first')
            state.save(root,name,{'state':'configuring','user_id':identity},exclusive=True)
            operation='budget'
            try:
                result=management('PUT','budgets',{'subject':login,'monthly_usd':monthly_usd})
                if not isinstance(result,dict) or set(result)!={'updated'} or result['updated'] is not True:raise ValueError('Budget update not confirmed')
                operation='key-issuance'
                result=management('POST','keys',{'subject':login,'name':'population-inference','expiresIn':key_lifetime})
                operation='key-validation'
                if (not isinstance(result,dict) or not isinstance(result.get('id'),str) or not 0<len(result['id'])<=128
                        or not isinstance(result.get('key'),str) or not 1<=len(result['key'])<=4096
                        or any(c.isspace() or not 33<=ord(c)<=126 for c in result['key'])):
                    raise ValueError('One-time key response invalid')
                operation='expiry'
                expiration=result.get('expirationDate')
                if not expiration:
                    listed=(oidc.list_keys if list_keys is None else list_keys)(profile)
                    matches=[row for row in listed if isinstance(row,dict) and row.get('id')==result['id']]
                    if len(matches)!=1 or str(matches[0].get('status','')).lower()!='active':
                        raise ValueError('Issued key was not uniquely confirmed active')
                    expiration=matches[0].get('expirationDate')
                expiry(expiration)
                operation='key-storage'
                fd=os.open(root/(identity+'.key'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as output:
                    output.write(result['key']+'\n');output.flush();os.fsync(output.fileno())
                state.save(root,name,dict(expected,key_id=result['id'],expires_at=expiration,
                    key_sha256=hashlib.sha256((result['key']+'\n').encode()).hexdigest()))
            except Exception:
                raise ValueError('Access setup uncertain at '+operation+'; preserve private state and reconcile before retrying') from None
        return {'configured_users':len(document['profiles']),'monthly_usd':monthly_usd,'key_lifetime':key_lifetime}
    finally:os.close(lock)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles',type=Path,required=True)
    parser.add_argument('--state',type=Path,default=Path('/root/enmaas-population-import'))
    parser.add_argument('--yes',action='store_true')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--configure-access',action='store_true',help='set test-user USD budgets and save finite inference keys privately')
    parser.add_argument('--monthly-usd',type=float,default=5)
    parser.add_argument('--key-lifetime',choices=('1h','24h'),default='24h')
    modes.add_argument('--revoke-all-keys',action='store_true',help='preview remaining keys of confirmed retired identities; --yes executes revocation')
    modes.add_argument('--retire',action='store_true',help='deactivate only confirmed imported identities and revoke partner-minted keys; other key paths require separate cleanup')
    args=parser.parse_args()
    if os.geteuid()!=0 or (not args.yes and not args.revoke_all_keys):raise ValueError('Run on the gateway with sudo and explicit --yes')
    if args.configure_access:
        result=configure_access(private.read(args.profiles),args.state,metering_admin.request,monthly_usd=args.monthly_usd,key_lifetime=args.key_lifetime)
    elif args.revoke_all_keys:
        result=clear_keys(private.read(args.profiles),args.state,metering_admin.request,execute=args.yes)
    else:
        result=apply(private.read(args.profiles),args.state,oidc.deactivate if args.retire else oidc.provision,retire=args.retire)
    print(json.dumps({**result,'private_state':str(args.state)},indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        raise SystemExit('Population import stopped; preserve private state and reconcile uncertain writes. No credential values printed.') from None
