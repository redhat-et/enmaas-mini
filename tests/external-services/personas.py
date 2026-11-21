#!/usr/bin/env python3
"""Provision the three lab dashboard personas through supported APIs only."""
import argparse
import fcntl
import importlib.util
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('persona_population',HERE/'import-population.py')
population=importlib.util.module_from_spec(spec);spec.loader.exec_module(population)
state=population.state
loader=importlib.machinery.SourceFileLoader('persona_api',str(HERE.parents[1]/'scripts/pricetag/api'))
spec=importlib.util.spec_from_loader(loader.name,loader)
api=importlib.util.module_from_spec(spec);loader.exec_module(api)
NAMES=('admin','alice','bob')
ROLES={'admin':'super_admin','alice':'admin','bob':'user'}


def validate(identities):
    if not isinstance(identities,dict) or set(identities)!=set(NAMES):
        raise ValueError('Expected exactly the three issuer persona UUIDs')
    for identity in identities.values():
        if not isinstance(identity,str) or str(uuid.UUID(identity))!=identity:
            raise ValueError('Expected canonical issuer UUID')
    if len(set(identities.values()))!=3:raise ValueError('Persona UUIDs must be distinct')
    return identities


def profiles(identities):
    validate(identities)
    return {name:{'user_id':identities[name],'tags':{'email':name+'@example.test',
                'first_name':name.title(),'last_name':'Lab'}} for name in NAMES}


def verify_users(rows,identities,*,complete=False):
    if not isinstance(rows,list):raise ValueError('Invalid managed-user response')
    if any(not isinstance(row,dict) or not isinstance(row.get('logins'),list) for row in rows):
        raise ValueError('Invalid managed-user login mapping')
    found={}
    for name,identity in identities.items():
        login=name+'@example.test'
        matches=[row for row in rows if isinstance(row,dict) and
                 (row.get('user_id')==identity or login in row.get('logins',[]))]
        if not matches:continue
        if len(matches)!=1:raise ValueError('Ambiguous persona identity')
        row=matches[0]
        if row.get('user_id')!=identity or row.get('logins')!=[login] or row.get('active') is not True:
            raise ValueError('Persona login/UUID differs or is inactive')
        if complete and (row.get('role')!=ROLES[name] or
                         row.get('manager_user_id')!=(identities['alice'] if name=='bob' else None)):
            raise ValueError('Persona access changed; reconcile explicitly')
        found[name]=row
    if complete and set(found)!=set(NAMES):raise ValueError('Persona identity missing')
    return found


def apply(identities,root,partner,management,public):
    """Durable intent receipts prevent replay of uncertain privilege writes."""
    expected=profiles(identities)
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
        state.save(root,'identities.json',identities,exclusive=True)
    directory=state.secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if state.load(root,'identities.json')!=identities:raise ValueError('Persona state belongs to another issuer')
        steps=[('import-'+name,name,'import') for name in NAMES]+[
            ('access-'+name,name,'access') for name in NAMES]+[
            ('budget-'+name,name,'budget') for name in ('alice','bob')]
        done=set();gap=False
        for step,_,_ in steps:
            try:receipt=state.load(root,step+'.json')
            except FileNotFoundError:gap=True;continue
            if gap or receipt!={'state':'done','step':step}:
                raise ValueError('Uncertain or out-of-order persona stage; reconcile before retrying')
            done.add(step)
        existing=verify_users(management('GET','users'),identities)
        if not done and existing:raise ValueError('Existing persona identities require explicit reconciliation')
        if any('import-'+name not in done for name in existing):
            raise ValueError('Unrecorded persona identity requires reconciliation')
        for name in NAMES:
            if 'import-'+name in done and name not in existing:
                raise ValueError('Confirmed persona import is missing')
        budgets=management('GET','budgets')
        if not isinstance(budgets,list) or any(not isinstance(row,dict) for row in budgets):
            raise ValueError('Invalid budget response')
        for name in ('alice','bob'):
            matches=[row for row in budgets if row.get('scope')=='user' and row.get('principal')==name+'@example.test']
            if 'budget-'+name not in done and matches:
                raise ValueError('Existing budget has no setup receipt')
        for step,name,kind in steps:
            if step in done:continue
            # A confirmed import must still have the same immutable identity.
            if kind!='import' and set(verify_users(management('GET','users'),identities))!=set(NAMES):
                raise ValueError('All issuer identities must exist before access changes')
            state.save(root,step+'.json',{'state':'started','step':step},exclusive=True)
            try:
                if kind=='import':
                    result=partner(expected[name])
                    if result.get('user_id')!=identities[name] or result.get('active') is not True:
                        raise ValueError('Import response mismatch')
                elif kind=='access':
                    result=public('PATCH','/api/v1/admin/partner-users/'+identities[name],
                                  {'role':ROLES[name],'manager_user_id':identities['alice'] if name=='bob' else None})
                    if result.get('user_id')!=identities[name] or result.get('role')!=ROLES[name] or result.get('manager_user_id')!=(identities['alice'] if name=='bob' else None):
                        raise ValueError('Access response mismatch')
                else:
                    if management('PUT','budgets',{'subject':name+'@example.test','monthly_usd':5})!={'updated':True}:
                        raise ValueError('Budget response mismatch')
            except Exception:
                raise ValueError('Persona stage uncertain: '+step+'; retain receipts and reconcile') from None
            state.save(root,step+'.json',{'state':'done','step':step})
        verify_users(management('GET','users'),identities,complete=True)
        budgets=management('GET','budgets')
        for name in ('alice','bob'):
            matches=[row for row in budgets if row.get('scope')=='user' and row.get('principal')==name+'@example.test']
            if len(matches)!=1 or matches[0].get('monthly_usd')!=5:
                raise ValueError('Persona budget differs; reconcile explicitly')
        return {'dashboard_personas_configured':True,'personas':list(NAMES),'inference_keys_created':False}
    finally:os.close(lock)


def public_client(origin):
    origin=api.session_origin('https://127.0.0.1:8443',origin)
    def call(method,path,body):
        with tempfile.TemporaryDirectory(prefix='enmaas-persona-') as temporary:
            root=Path(temporary);state.save(root,'input.json',body,exclusive=True)
            command=[sys.executable,str(HERE.parents[1]/'scripts/pricetag/api'),method,path,
                '--url','https://127.0.0.1:8443','--origin',origin,
                '--key-file','/root/pricetag-admin/admin.key','--ca-file','/etc/praxis-pricetag/ca.pem',
                '--input',str(root/'input.json'),'--output',str(root/'output.json')]
            result=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90)
            if result.returncode:
                raise ValueError('Authenticated persona operation failed')
            response=state.load(root,'output.json')
        return response
    return call


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identities',required=True,type=Path)
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--origin',required=True)
    parser.add_argument('--yes',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or not args.yes:raise ValueError('Requires root and explicit --yes')
    result=apply(population.private.read(args.identities),args.state,population.oidc.provision,
                 population.metering_admin.request,public_client(args.origin))
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError,KeyError):
        raise SystemExit('Persona setup stopped; inspect private receipts. No credential diagnostics printed.') from None
