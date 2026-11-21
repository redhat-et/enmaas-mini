#!/usr/bin/env python3
"""Create a dedicated VPC and three restricted RHEL hosts; preview by default.

Uses the selected operator profile. No services, DNS, RDS or cleanup are implied.
On failure retain journals and resources; never rerun with a fresh journal blindly.
"""
import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import subprocess

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('infra_network',HERE/'vpc-create.py')
network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)
registry=network.planner.registry
loader=importlib.machinery.SourceFileLoader('infra_vm',str(HERE.parents[1]/'scripts/aws/rhel-vm'))
spec=importlib.util.spec_from_loader(loader.name,loader)
vm=importlib.util.module_from_spec(spec);loader.exec_module(vm)
HOSTS={
    'gateway':('remote-gateway','m7i.2xlarge',150),
    'keycloak':('keycloak','m7i.large',50),
    'observability':('observability','m7i.xlarge',100),
}


def preview(manifest,*,cidr,zones,allowed_cidr,image,journal):
    plan=network.planner.build_plan(manifest,cidr,zones)
    if not re.fullmatch(r'ami-[a-f0-9]{8,17}',image):
        raise ValueError('Supply an exact approved AMI ID')
    hosts={}
    for role,(scenario,instance_type,volume) in HOSTS.items():
        hosts[role]={'scenario':scenario,'instance_type':instance_type,'volume_gib':volume,
            'image':image,'subnet':plan['placement'][role],
            'ssh_access':'restricted','https_access':'restricted',
            'ingress':vm.ingress(scenario,allowed_cidr),
            'journal':str(journal/(manifest['id']+'-'+role+'.json'))}
    return {'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
        'profile':manifest['aws_profile'],'network':plan,'hosts':hosts,
        'ssh_key_path':manifest['ssh_key_path'],'journal':str(journal),
        'runtime_verified':False,'services_installed':False,
        'dns':{'zone_id':manifest['zone_id'],'domain':manifest['domain'],'records_created':False},
        'pending':['private peer rules','optional RDS and DNS','services and TLS','AWS/RHEL security qualification']}


def check_public_key(path):
    try:
        key=path.read_text()
        if ('PRIVATE KEY' in key or len(key.strip().splitlines())!=1
                or key.split()[0] not in ('ssh-ed25519','ssh-rsa')):
            raise ValueError('Invalid public key')
        result=subprocess.run(['ssh-keygen','-lf',str(path)],capture_output=True,timeout=10)
        if result.returncode:raise ValueError('Invalid public key')
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise ValueError('Supply a valid RSA/Ed25519 public key beside the selected SSH key (.pub)') from None


def deploy(manifest,*,cidr,zones,allowed_cidr,image,journal,confirm):
    plan=preview(manifest,cidr=cidr,zones=zones,allowed_cidr=allowed_cidr,image=image,journal=journal)
    if confirm!='deploy '+manifest['id']:
        raise ValueError('Exact deployment confirmation required')
    public_key=Path(manifest['ssh_key_path']+'.pub')
    check_public_key(public_key)
    ledger=network.Journal(journal)
    ledger.append({'event':'plan','plan':plan})
    resources=network.create(network.Aws(manifest),manifest,cidr,zones,journal/'network',
                             'create network '+manifest['id'])
    ledger.append({'event':'network-created','resources':resources})
    client=vm.Aws(manifest['region'],manifest['aws_profile'],True)
    prepared={}
    # Resolve and validate all three placements before launching the first host.
    for role,host in plan['hosts'].items():
        args=argparse.Namespace(config=None,scenario=host['scenario'],inference='none',arch='amd64',
            instance_type=host['instance_type'],volume_gib=host['volume_gib'],ami_id=image,
            ssh_access='restricted',https_access='restricted',region=manifest['region'],
            account_id=manifest['account'],prefix=manifest['id']+'-'+role,public_key=public_key,
            allowed_cidr=allowed_cidr,subnet_id=resources['subnets'][host['subnet']],
            state_file=journal/(manifest['id']+'-'+role+'.json'))
        vm.configure(args)
        proposal=vm.plan(client,args)
        if proposal['VpcId']!=resources['vpc']:
            raise ValueError('Host placement escaped the newly created VPC')
        prepared[role]=(args,proposal)
    ledger.append({'event':'hosts-planned','roles':list(prepared)})
    result={}
    for role,(args,proposal) in prepared.items():
        ledger.append({'event':'host-launch-intent','role':role,'journal':str(args.state_file)})
        with vm.deployment_lock(args.state_file):
            vm.apply_plan(client,args,proposal)
        result[role]=registry.read_private_json(args.state_file)
        ledger.append({'event':'host-launch-recorded','role':role,'journal':str(args.state_file)})
    ledger.append({'event':'launches-recorded','runtime_verified':False})
    return {'deployment':manifest['id'],'network':resources,'hosts':result,
            'journal':str(journal),'runtime_verified':False,'services_installed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--manifest',type=Path)
    selection.add_argument('--group',help='register a new deployment while creating its infrastructure')
    parser.add_argument('--account')
    parser.add_argument('--region')
    parser.add_argument('--profile')
    parser.add_argument('--ssh-key',type=Path)
    parser.add_argument('--dns-zone-id',default='',help='optional existing public hosted-zone ID for a new deployment')
    parser.add_argument('--dns-domain',default='',help='zone domain paired with --dns-zone-id; records are created separately after launch')
    parser.add_argument('--registry-dir',type=Path,default=HERE.parents[1]/'.state/deployments')
    parser.add_argument('--cidr',required=True)
    parser.add_argument('--zone',action='append',required=True)
    parser.add_argument('--allowed-cidr',required=True)
    parser.add_argument('--image',required=True)
    parser.add_argument('--journal',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--confirm')
    args=parser.parse_args()
    if args.manifest:
        if any((args.account,args.region,args.profile,args.ssh_key,args.dns_zone_id,args.dns_domain)):
            raise ValueError('Existing manifests supply account, region, profile, SSH key and DNS scope; do not override them')
        manifest=registry.load(args.manifest)
    else:
        if not all((args.account,args.profile,args.ssh_key)):
            raise ValueError('A new deployment requires --account, --profile and --ssh-key')
        manifest=registry.new(args.account,args.region or 'eu-central-1',str(args.ssh_key),args.profile,
                              zone_id=args.dns_zone_id,domain=args.dns_domain,group=args.group)
    options=dict(cidr=args.cidr,zones=args.zone,allowed_cidr=args.allowed_cidr,
                 image=args.image,journal=args.journal)
    if args.group:
        result=preview(manifest,**options)
        result.update(registered=False,manifest=str(args.registry_dir/(manifest['id']+'.json')))
        if args.apply:
            if args.confirm!='deploy '+args.group+' '+args.account:
                raise ValueError('Exact group/account deployment confirmation required')
            if args.journal.exists() or args.journal.is_symlink():
                raise FileExistsError('Existing infrastructure journal; inspect it instead of creating another deployment')
            check_public_key(Path(manifest['ssh_key_path']+'.pub'))
            saved=registry.save(args.registry_dir,manifest)
            print('Recovery manifest:',saved,flush=True)
            result=deploy(manifest,**options,confirm='deploy '+manifest['id'])
            result.update(registered=True,manifest=str(saved))
    else:
        result=deploy(manifest,**options,confirm=args.confirm) if args.apply else preview(manifest,**options)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as error:raise SystemExit('Infrastructure stopped: '+str(error)) from None
