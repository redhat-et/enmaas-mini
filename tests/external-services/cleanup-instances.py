#!/usr/bin/env python3
"""Preview or request termination of three verified lab VMs; retain other resources.

Explicitly destroys their local data. Does not remove DNS, RDS, network resources,
keys, journals or backups. No implicit retries after an uncertain AWS response.
"""
import argparse
import importlib.util
import json
import os
import stat
from pathlib import Path
import re
import subprocess

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('instance_inventory',HERE/'cleanup-inventory.py')
inventory=importlib.util.module_from_spec(spec);spec.loader.exec_module(inventory)
spec=importlib.util.spec_from_file_location('cleanup_journal',HERE/'vpc-create.py')
ledger=importlib.util.module_from_spec(spec);spec.loader.exec_module(ledger)


class Aws:
    WRITES={('ec2','terminate-instances')}
    READS={('sts','get-caller-identity'),*[('ec2',name) for name in (
        'describe-instances','describe-security-groups','describe-key-pairs','describe-volumes','describe-network-interfaces')]}
    def __init__(self,manifest,apply=False):self.manifest=manifest;self.apply=apply
    def call(self,service,action,**options):
        if (service,action) not in self.READS and not (self.apply and (service,action) in self.WRITES):
            raise ValueError('Cleanup operation not permitted')
        command=['aws','--no-cli-pager','--profile',self.manifest['aws_profile'],
            '--region',self.manifest['region'],'--output','json',service,action,
            '--cli-input-json',json.dumps(options)]
        try:
            r=subprocess.run(command,capture_output=True,text=True,timeout=60,
                env=inventory.vm._environment.environment(self.manifest['aws_profile']))
            if r.returncode:raise ValueError()
            result=json.loads(r.stdout or '{}')
            if not isinstance(result,dict):raise ValueError()
            return result
        except (OSError,ValueError,subprocess.TimeoutExpired):
            raise ValueError('AWS cleanup call failed or timed out; inspect its journal before retrying') from None


def verified_snapshot(aws,manifest,journals):
    inventory.registry.validate(manifest)
    inventory.vm.check_identity(aws,manifest['account'])
    # Inventory uses snake-case options with its shared client; normalize to AWS JSON keys.
    class Reader:
        def call(self,service,action,**options):
            return aws.call(service,action,**{''.join(p.title() for p in key.split('_')):value for key,value in options.items()})
    reader=Reader()
    resources=inventory.collect_resources(reader,manifest)
    rows=inventory.inventory(manifest,journals,resources)
    groups=set(resources['groups']);instances=set(resources['instances'])
    dependencies=inventory.network_dependencies(reader,manifest['account'],groups,instances)
    if any(row['relationship']!='journaled VM' or not row['delete_with_instance'] for row in dependencies):
        raise ValueError('Unresolved network interfaces; reconcile before termination')
    vpcs=set()
    for identifier,instance in resources['instances'].items():
        if not re.fullmatch(r'i-[a-f0-9]{8,17}',identifier) or instance.get('State',{}).get('Name') not in ('running','stopped'):
            raise ValueError('Require stable running/stopped journaled instances')
        vpc=instance.get('VpcId','')
        if not re.fullmatch(r'vpc-[a-f0-9]{8,17}',vpc):raise ValueError('Invalid VPC identity')
        recorded=[state for state in journals.values() if state.get('InstanceId')==identifier]
        if (len(recorded)!=1 or recorded[0].get('VpcId')!=vpc
                or not instance.get('SubnetId') or recorded[0].get('SubnetId')!=instance['SubnetId']):
            raise ValueError('Instance network differs from its creation journal')
        vpcs.add(vpc)
        interfaces=instance.get('NetworkInterfaces',[])
        if len(interfaces)!=1 or interfaces[0].get('Attachment',{}).get('DeviceIndex')!=0:
            raise ValueError('Unexpected instance network attachments')
        interface=interfaces[0]
        matches=[row for row in dependencies if row['id']==interface.get('NetworkInterfaceId') and row['instance']==identifier]
        if len(matches)!=1:raise ValueError('Missing or ambiguous primary interface')
    if len(vpcs)!=1:raise ValueError('Instances do not share the recorded deployment network')
    return {'instances':sorted(instances),'root_volumes':sorted(row['id'] for row in rows if row['kind']=='volumes'),
            'vpc':next(iter(vpcs))}


def cleanup_policy(manifest,journals):
    """Render an optional exact-instance policy; never attach it or contact AWS."""
    inventory.registry.validate(manifest)
    statements=[{'Sid':'CleanupDiscovery','Effect':'Allow',
        'Action':['ec2:'+''.join(part.title() for part in action.split('-'))
                  for service,action in sorted(Aws.READS) if service=='ec2'],
        'Resource':['*'],'Condition':{'StringEquals':{'aws:RequestedRegion':manifest['region']}}}]
    seen=set()
    for role,scenario in inventory.ROLES.items():
        state=journals[role];prefix=manifest['id']+'-'+role
        if any(state.get(key)!=value for key,value in {'AccountId':manifest['account'],
                'Region':manifest['region'],'Prefix':prefix,'Scenario':scenario}.items()):
            raise ValueError('Cleanup policy journal differs from deployment')
        instance=state.get('InstanceId','')
        if not re.fullmatch(r'i-[a-f0-9]{8,17}',instance) or instance in seen:
            raise ValueError('Cleanup policy requires three unique exact instance IDs')
        seen.add(instance)
        condition={'aws:RequestedRegion':manifest['region'],
            **{'aws:ResourceTag/'+tag['Key']:tag['Value'] for tag in inventory.vm.tags(prefix,scenario)}}
        statements.append({'Sid':'Terminate'+role.title(),'Effect':'Allow','Action':['ec2:TerminateInstances'],
            'Resource':['arn:aws:ec2:'+manifest['region']+':'+manifest['account']+':instance/'+instance],
            'Condition':{'StringEquals':condition}})
    return {'Version':'2012-10-17','Statement':statements}


def required_confirmation(manifest):
    return 'delete VM data '+manifest['id']+' '+manifest['account']+' '+manifest['region']


def execute(aws,manifest,journals,journal,*,apply,confirmation):
    inventory.registry.validate(manifest)
    expected=required_confirmation(manifest)
    if apply and confirmation!=expected:raise ValueError('Exact VM data-loss confirmation required')
    snapshot=verified_snapshot(aws,manifest,journals)
    result={'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
        **snapshot,'termination_requested':False,'termination_verified':False,
        'confirmation':expected,'remaining':['DNS','security groups and key pairs','optional RDS/backups',
            'dedicated VPC dependencies','local keys and journals']}
    if not apply:return result
    journal=ledger.Journal(journal)
    journal.append({'event':'plan','result':result})
    if verified_snapshot(aws,manifest,journals)!=snapshot:
        raise ValueError('Live ownership changed; no termination requested')
    journal.append({'event':'intent','action':'terminate-instances','instances':snapshot['instances']})
    try:
        response=aws.call('ec2','terminate-instances',InstanceIds=snapshot['instances'])
        rows=response['TerminatingInstances']
        if (len(rows)!=3 or {r['InstanceId'] for r in rows}!=set(snapshot['instances'])
                or any(r['CurrentState']['Name'] not in ('shutting-down','terminated') for r in rows)):
            raise ValueError()
    except (ValueError,OSError,KeyError,TypeError):
        raise ValueError('Termination outcome uncertain; inspect AWS and the existing journal, do not repeat with a new journal') from None
    journal.append({'event':'request-recorded','action':'terminate-instances','instances':snapshot['instances']})
    result['termination_requested']=True
    return result


def verify_termination(aws,manifest,journals,path):
    """Read the same termination journal and observe results; never retry a write."""
    cleanup_policy(manifest,journals)  # Validate exact source journal ownership/IDs.
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('Cleanup journal must be a private owned directory')
    names=sorted(entry.name for entry in path.iterdir())
    if names not in (['0000.json','0001.json'],['0000.json','0001.json','0002.json']):
        raise ValueError('Expected a recorded termination intent; preserve incomplete records')
    events=[inventory.registry.read_private_json(path/name) for name in names]
    result=events[0].get('result',{})
    instances=sorted(row['InstanceId'] for row in journals.values())
    expected={'deployment':manifest['id'],'account':manifest['account'],
              'region':manifest['region'],'instances':instances}
    if (events[0].get('event')!='plan' or any(result.get(k)!=v for k,v in expected.items())
            or events[1]!={'event':'intent','action':'terminate-instances','instances':instances}):
        raise ValueError('Cleanup intent belongs to another deployment')
    if len(events)==3 and events[2]!={'event':'request-recorded','action':'terminate-instances','instances':instances}:
        raise ValueError('Cleanup result differs from its intent')
    volumes=result.get('root_volumes',[])
    if (len(volumes)!=3 or len(set(volumes))!=3
            or any(not re.fullmatch(r'vol-[a-f0-9]{8,17}',value) for value in volumes)):
        raise ValueError('Missing exact recorded root volume IDs')
    inventory.vm.check_identity(aws,manifest['account'])
    response=aws.call('ec2','describe-instances',Filters=[{'Name':'instance-id','Values':instances}])
    if response.get('NextToken'):raise ValueError('Incomplete instance observation')
    observed={}
    for reservation in response['Reservations']:
        if reservation.get('OwnerId')!=manifest['account']:raise ValueError('Foreign instance observation')
        for row in reservation['Instances']:
            identity=row['InstanceId']
            if identity not in instances or identity in observed:raise ValueError('Unexpected or duplicate instance')
            source=next(state for state in journals.values() if state['InstanceId']==identity)
            if not inventory.vm.owned_tags(row,source['Prefix'],source['Scenario']):
                raise ValueError('Instance ownership differs during cleanup')
            observed[identity]=row['State']['Name']
    disks=aws.call('ec2','describe-volumes',Filters=[{'Name':'volume-id','Values':volumes}])
    remaining=[row['VolumeId'] for row in disks['Volumes']]
    if disks.get('NextToken') or len(remaining)!=len(set(remaining)) or not set(remaining)<=set(volumes):
        raise ValueError('Incomplete or unexpected volume observation')
    return {**expected,'termination_verified':len(observed)==3 and set(observed.values())=={'terminated'} and not remaining,
        'instance_states':observed,'unobserved_instances':sorted(set(instances)-set(observed)),
        'remaining_root_volumes':remaining,'writes_performed':False,
        'remaining':['DNS','network interfaces, security groups and key pairs','optional RDS/backups',
                     'dedicated VPC dependencies','local keys and journals']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--journal',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--verify',action='store_true',help='observe the same termination journal without retrying writes')
    mode.add_argument('--policy',action='store_true',help='render optional exact-instance IAM policy offline; never attach it')
    parser.add_argument('--confirm')
    args=parser.parse_args()
    manifest=inventory.registry.load(args.manifest)
    journals={role:inventory.registry.read_private_json(args.state_dir/(manifest['id']+'-'+role+'.json')) for role in inventory.ROLES}
    if args.policy:
        print(json.dumps(cleanup_policy(manifest,journals),indent=2))
        return
    if not args.journal:raise ValueError('Supply a dedicated cleanup journal')
    if args.verify:
        result=verify_termination(Aws(manifest),manifest,journals,args.journal)
    else:
        result=execute(Aws(manifest,args.apply),manifest,journals,args.journal,apply=args.apply,confirmation=args.confirm)
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,TypeError):
        raise SystemExit('Cleanup stopped; preserve journals and inspect live state before retrying. No other resource types are removed.') from None
