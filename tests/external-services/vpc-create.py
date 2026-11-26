#!/usr/bin/env python3
"""Create a dedicated network with durable intent records; never retry uncertain writes."""
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import argparse
import subprocess

spec=importlib.util.spec_from_file_location('network_planner',Path(__file__).with_name('vpc-plan.py'))
planner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class Aws(planner.Aws):
    ACTIONS={'create-vpc','modify-vpc-attribute','create-internet-gateway',
             'attach-internet-gateway','create-route-table','create-route',
             'create-subnet','modify-subnet-attribute','associate-route-table'}

    def execute(self,arguments,timeout=60):
        command=['aws','--no-cli-pager','--profile',self.manifest['aws_profile'],
                 '--region',self.manifest['region'],'--output','json']+arguments
        environment=planner.aws_environment.environment(self.manifest['aws_profile'])
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,env=environment)
            if result.returncode:raise ValueError('AWS request failed')
            value=json.loads(result.stdout) if result.stdout.strip() else {}
            if not isinstance(value,dict):raise ValueError('Invalid AWS response')
            return value
        except (OSError,ValueError,subprocess.TimeoutExpired):
            raise ValueError('AWS network request failed or timed out; inspect the journal before retrying') from None

    def call(self,service,action,**options):
        if (service,action) in {('sts','get-caller-identity'),
                               ('ec2','describe-vpcs'),('ec2','describe-availability-zones')}:
            return super().call(service,action)
        if service!='ec2' or action not in self.ACTIONS:
            raise ValueError('AWS action not allowed')
        return self.execute([service,action,'--cli-input-json',json.dumps(options)])

    def wait(self,kind,identifier):
        if kind not in ('vpc','subnet') or not re.fullmatch(kind+r'-[a-f0-9]{8,17}',identifier):
            raise ValueError('Invalid network waiter resource')
        self.execute(['ec2','wait',kind+'-available','--'+kind+'-ids',identifier],timeout=180)


class Journal:
    def __init__(self,path):
        self.path=Path(path)
        info=self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('Network journal parent must be a private user-owned directory')
        self.path.mkdir(mode=0o700)  # Exclusive claim: never overwrite or resume implicitly.
        fd=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
        self.sequence=0

    def append(self,event):
        path=self.path/(str(self.sequence).zfill(4)+'.json')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(event,stream,indent=2)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        fd=os.open(self.path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
        self.sequence+=1


def create(aws,manifest,cidr,zones,path,confirmation):
    planner.registry.validate(manifest)
    if confirmation!='create network '+manifest['id']:
        raise ValueError('Exact deployment confirmation is required')
    plan=planner.live_plan(aws,manifest,cidr,zones)
    journal=Journal(path)
    journal.append({'event':'plan','manifest':manifest,'plan':plan})

    def operation(action,options,fields=(),prefix=None):
        journal.append({'event':'intent','action':action,'options':options})
        try:
            response=aws.call('ec2',action,**options)
            value=response
            for field in fields:value=value[field]
            if prefix is not None and not re.fullmatch(prefix+r'-[a-f0-9]{8,17}',value):
                raise ValueError('Invalid resource identifier')
            if action=='create-route' and response.get('Return') is not True:
                raise ValueError('Route was not accepted')
        except (OSError,ValueError,KeyError,TypeError):
            raise ValueError('Network operation outcome is uncertain; inspect the journal and tagged resources before retrying') from None
        journal.append({'event':'result','action':action,'id':value if prefix else None})
        return value

    def tags(kind,name):
        return [{'ResourceType':kind,'Tags':plan['tags']+[{'Key':'Name','Value':manifest['id']+'-'+name}]}]

    vpc=operation('create-vpc',{'CidrBlock':cidr,'InstanceTenancy':'default','TagSpecifications':tags('vpc','vpc')},('Vpc','VpcId'),'vpc')
    aws.wait('vpc',vpc)
    for attribute in ('EnableDnsSupport','EnableDnsHostnames'):
        operation('modify-vpc-attribute',{'VpcId':vpc,attribute:{'Value':True}})
    gateway=operation('create-internet-gateway',{'TagSpecifications':tags('internet-gateway','internet')},
                      ('InternetGateway','InternetGatewayId'),'igw')
    operation('attach-internet-gateway',{'VpcId':vpc,'InternetGatewayId':gateway})
    tables={}
    for kind in ('public','private'):
        tables[kind]=operation('create-route-table',{'VpcId':vpc,'TagSpecifications':tags('route-table',kind)},
                               ('RouteTable','RouteTableId'),'rtb')
    operation('create-route',{'RouteTableId':tables['public'],'DestinationCidrBlock':'0.0.0.0/0','GatewayId':gateway})
    subnets={};associations={}
    for subnet in plan['subnets']:
        key=subnet['key']
        identifier=operation('create-subnet',{'VpcId':vpc,'CidrBlock':subnet['cidr'],
            'AvailabilityZoneId':plan['zone_ids'][subnet['zone']],'TagSpecifications':tags('subnet',key)},
            ('Subnet','SubnetId'),'subnet')
        subnets[key]=identifier
        aws.wait('subnet',identifier)
        operation('modify-subnet-attribute',{'SubnetId':identifier,'MapPublicIpOnLaunch':{'Value':False}})
        associations[key]=operation('associate-route-table',{'SubnetId':identifier,'RouteTableId':tables[subnet['route_table']]},
                                    ('AssociationId',),'rtbassoc')
    resources={'vpc':vpc,'internet_gateway':gateway,'route_tables':tables,'subnets':subnets,'associations':associations}
    journal.append({'event':'complete','resources':resources,'runtime_verified':False})
    return resources


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--cidr',required=True)
    parser.add_argument('--zone',action='append',required=True)
    parser.add_argument('--apply',action='store_true',help='create network resources after live preflight and exact confirmation')
    parser.add_argument('--journal',type=Path,help='new journal directory under a private parent; never overwritten')
    parser.add_argument('--confirm',help='exact text: create network DEPLOYMENT_ID')
    args=parser.parse_args()
    manifest=planner.registry.load(args.manifest)
    if args.apply:
        if not args.journal or not args.confirm:raise ValueError('--apply requires --journal and --confirm')
        resources=create(Aws(manifest),manifest,args.cidr,args.zone,args.journal,args.confirm)
        result={'resources':resources,'journal':str(args.journal),'runtime_verified':False}
    else:
        result=planner.build_plan(manifest,args.cidr,args.zone)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as error:raise SystemExit('Network creation stopped: '+str(error)) from None
