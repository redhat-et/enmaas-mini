#!/usr/bin/env python3
"""Remove exact imported EC2 key-pair resources after verified VM termination.

Preview by default. Never removes local private keys, changes authorized_keys on
hosts, or deletes network/database resources. Uncertain writes stop for review.
"""
import argparse
import importlib.util
import json
import os
import stat
from pathlib import Path
import re

spec=importlib.util.spec_from_file_location('key_termination',Path(__file__).with_name('cleanup-instances.py'))
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)


class Aws(t.Aws):
    WRITES={('ec2','delete-key-pair')}


def recorded_keys(manifest,journals):
    t.cleanup_policy(manifest,journals)
    if set(journals)!=set(t.inventory.ROLES):raise ValueError('Expected exactly three source journals')
    expected={}
    for role,state in journals.items():
        identity=state.get('KeyPairId','')
        if (not isinstance(identity,str) or not re.fullmatch(r'key-[a-f0-9]{8,17}',identity)
                or identity in expected or state.get('KeyPairName')!=manifest['id']+'-'+role):
            raise ValueError('Exact unique imported key IDs and names required; do not adopt legacy keys')
        expected[identity]=state
    return expected


def snapshot(aws,manifest,journals,*,deleted=()):
    expected=recorded_keys(manifest,journals)
    if not set(deleted)<=set(expected):raise ValueError('Unexpected deleted key IDs')
    expected={identity:state for identity,state in expected.items() if identity not in deleted}
    response=aws.call('ec2','describe-key-pairs',Filters=[{'Name':'key-pair-id','Values':sorted(expected)}])
    keys=response['KeyPairs']
    if response.get('NextToken') or len(keys)!=len(expected) or {row['KeyPairId'] for row in keys}!=set(expected):
        raise ValueError('Missing, duplicate, foreign or incomplete key inventory')
    for row in keys:
        state=expected[row['KeyPairId']]
        if row['KeyName']!=state['KeyPairName'] or not t.inventory.vm.owned_tags(row,state['Prefix'],state['Scenario']):
            raise ValueError('Imported key ownership differs')
    names={state['KeyPairName'] for state in expected.values()}
    response=aws.call('ec2','describe-instances',Filters=[{'Name':'key-name','Values':sorted(names)}])
    if response.get('NextToken'):raise ValueError('Incomplete key usage inventory')
    for reservation in response['Reservations']:
        if reservation['OwnerId']!=manifest['account']:raise ValueError('Foreign key usage inventory')
        for instance in reservation['Instances']:
            if instance.get('KeyName') not in names or instance.get('State',{}).get('Name')!='terminated':
                raise ValueError('Key is still used or instance inventory differs')
    return sorted(expected)


def required_confirmation(manifest):
    return 'delete EC2 keys '+manifest['id']+' '+manifest['account']+' '+manifest['region']


def execute(aws,manifest,journals,termination_journal,journal,*,apply,confirmation):
    expected=required_confirmation(manifest)
    if apply and confirmation!=expected:raise ValueError('Exact key cleanup confirmation required')
    result=t.verify_termination(aws,manifest,journals,termination_journal)
    if result['termination_verified'] is not True:
        raise ValueError('VM termination and root-volume removal must be verified first')
    keys=snapshot(aws,manifest,journals)
    result={'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
            'key_pair_ids':keys,'deletion_requested':False,'deletion_verified':False,
            'confirmation':expected,'local_keys_retained':True}
    if not apply:return result
    records=t.ledger.Journal(journal)
    records.append({'event':'plan','result':result})
    for index,identity in enumerate(keys):
        # Repeat ownership/usage checks before every mutation, omitting already
        # removed keys. A changed remaining inventory stops the sequence.
        if snapshot(aws,manifest,journals,deleted=keys[:index])!=keys[index:]:
            raise ValueError('Remaining key inventory changed; reconcile recorded deletions')
        records.append({'event':'intent','action':'delete-key-pair','key_pair_id':identity})
        try:
            response=aws.call('ec2','delete-key-pair',KeyPairId=identity)
            if response.get('Return') is not True or response.get('KeyPairId')!=identity:raise ValueError()
        except (ValueError,OSError,KeyError,TypeError):
            raise ValueError('Key deletion outcome uncertain; preserve the journal and reconcile, do not replay') from None
        records.append({'event':'request-recorded','action':'delete-key-pair','key_pair_id':identity})
    result['deletion_requested']=True
    return result


def policy(manifest,journals):
    expected=recorded_keys(manifest,journals)
    statements=[t.cleanup_policy(manifest,journals)['Statement'][0]]
    for index,state in enumerate(expected.values()):
        statements.append({'Sid':'DeleteKey'+str(index),'Effect':'Allow','Action':['ec2:DeleteKeyPair'],
            'Resource':['arn:aws:ec2:'+manifest['region']+':'+manifest['account']+':key-pair/'+state['KeyPairName']],
            'Condition':{'StringEquals':{'aws:RequestedRegion':manifest['region'],
                **{'aws:ResourceTag/'+tag['Key']:tag['Value'] for tag in
                   t.inventory.vm.tags(state['Prefix'],state['Scenario'])}}}})
    return {'Version':'2012-10-17','Statement':statements}


def verify(aws,manifest,journals,path):
    identities=sorted(recorded_keys(manifest,journals))
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('Key cleanup journal must be a private owned directory')
    names=sorted(entry.name for entry in path.iterdir())
    if not 2<=len(names)<=7 or names!=[str(i).zfill(4)+'.json' for i in range(len(names))]:
        raise ValueError('Expected a contiguous key cleanup intent journal')
    events=[t.inventory.registry.read_private_json(path/name) for name in names]
    binding={'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
             'key_pair_ids':identities}
    if events[0].get('event')!='plan' or any(events[0].get('result',{}).get(key)!=value for key,value in binding.items()):
        raise ValueError('Key cleanup journal belongs to another deployment')
    for index,event in enumerate(events[1:]):
        expected={'event':'intent' if index%2==0 else 'request-recorded',
                  'action':'delete-key-pair','key_pair_id':identities[index//2]}
        if event!=expected:raise ValueError('Key cleanup intent/result sequence differs')
    t.inventory.vm.check_identity(aws,manifest['account'])
    response=aws.call('ec2','describe-key-pairs',Filters=[{'Name':'key-pair-id','Values':identities}])
    remaining=[row['KeyPairId'] for row in response['KeyPairs']]
    if response.get('NextToken') or len(set(remaining))!=len(remaining) or not set(remaining)<=set(identities):
        raise ValueError('Incomplete or unexpected remaining key inventory')
    return {**binding,'remaining_key_pair_ids':sorted(remaining),'deletion_verified':not remaining,
            'writes_performed':False,'local_keys_retained':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--termination-journal',type=Path)
    parser.add_argument('--journal',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--verify',action='store_true')
    mode.add_argument('--policy',action='store_true')
    parser.add_argument('--confirm')
    args=parser.parse_args()
    if args.apply and args.journal is None:raise ValueError('Apply requires a new private key cleanup journal')
    manifest=t.inventory.registry.load(args.manifest)
    journals={role:t.inventory.registry.read_private_json(args.state_dir/(manifest['id']+'-'+role+'.json'))
              for role in t.inventory.ROLES}
    if args.policy:result=policy(manifest,journals)
    elif args.verify:
        if args.journal is None:raise ValueError('Verify requires the existing cleanup journal')
        result=verify(Aws(manifest),manifest,journals,args.journal)
    else:
        if args.termination_journal is None:raise ValueError('Require the VM termination journal')
        result=execute(Aws(manifest,args.apply),manifest,journals,args.termination_journal,args.journal,
                       apply=args.apply,confirmation=args.confirm)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        raise SystemExit('EC2 key cleanup stopped; preserve journals and reconcile live state. Local private keys remain unchanged.') from None
