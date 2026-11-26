#!/usr/bin/env python3
"""Preview a new deployment-owned VPC; optional read-only AWS preflight."""
import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import subprocess

spec=importlib.util.spec_from_file_location('vpc_registry',Path(__file__).with_name('deployments.py'))
registry=importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


spec=importlib.util.spec_from_file_location('vpc_aws_environment',Path(__file__).resolve().parents[2]/'scripts/aws/environment.py')
aws_environment=importlib.util.module_from_spec(spec);spec.loader.exec_module(aws_environment)

def build_plan(manifest,cidr,zones):
    registry.validate(manifest)
    if manifest['version']!=2:
        raise ValueError('A new deployment group manifest is required; existing networks are not adopted')
    network=ipaddress.IPv4Network(cidr,strict=True)
    private=tuple(ipaddress.IPv4Network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
    if not 16<=network.prefixlen<=21 or not any(network.subnet_of(n) for n in private):
        raise ValueError('Choose a canonical RFC1918 IPv4 VPC CIDR from /16 through /21')
    if (not 2<=len(zones)<=3 or len(set(zones))!=len(zones)
            or any(not re.fullmatch(re.escape(manifest['region'])+r'[a-z]',zone) for zone in zones)):
        raise ValueError('Choose two or three distinct standard Availability Zones in the selected region')
    tags=[{'Key':'enmaas:group','Value':manifest['group']},
          {'Key':'enmaas:run','Value':manifest['id']},
          {'Key':'enmaas:purpose','Value':'external-services-test'}]
    blocks=iter(network.subnets(new_prefix=24))
    subnets=[]
    for purpose,route_table in (('services','public'),('database','private')):
        for index,zone in enumerate(zones):
            subnets.append({'key':purpose+'-'+str(index+1),'zone':zone,'cidr':str(next(blocks)),
                            'purpose':purpose,'route_table':route_table,'map_public_ip_on_launch':False})
    roles=('gateway','keycloak','observability')
    return {
        'version':1,'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
        'changes_made':False,'aws_validated':False,'tags':tags,
        'vpc':{'cidr':str(network),'tenancy':'default','enable_dns_support':True,'enable_dns_hostnames':True},
        'subnets':subnets,'placement':{role:'services-'+str(index%len(zones)+1) for index,role in enumerate(roles)},
        'internet_gateway':{'new':True},'main_route_table':'local-only',
        'route_tables':{
            'public':{'routes':[{'destination':'0.0.0.0/0','target':'internet-gateway'}]},
            'private':{'routes':[]}},
        'ingress':{'ssh':'operator source CIDR only','client_https':'explicit restricted/public choice',
                   'peer_services':'exact security-group references; private addresses',
                   'database':'gateway security group only; no public endpoint'},
        'public_addresses':'explicit VM launch setting; public route alone does not assign an address',
        'cleanup':{'execution_enabled':False,'reject_default_or_unjournaled_vpc':True,
                   'order':['stop workloads; retain approved backups',
                            'remove journaled instances and database; wait for their interfaces',
                            'reject foreign or unjournaled attachments and group references',
                            'remove owned security groups and explicit route-table associations',
                            'delete journaled subnets and custom route tables',
                            'detach and delete journaled internet gateway',
                            'delete journaled nondefault VPC; retain audit journals']},
        'qualification_pending':['live account/zone availability and CIDR-overlap checks',
                                 'scoped IAM authorization and deny tests',
                                 'journaled creation, uncertain-result recovery and cleanup executor',
                                 'RHEL private connectivity, service TLS and ingress denial tests'],
    }


class Aws:
    """Read-only client, with CLI pagination enabled and diagnostic redaction."""
    def __init__(self,manifest):self.manifest=registry.validate(manifest)

    def call(self,service,action):
        if (service,action) not in {('sts','get-caller-identity'),
                ('ec2','describe-availability-zones'),('ec2','describe-vpcs')}:
            raise ValueError('AWS action not allowed')
        command=['aws','--no-cli-pager','--profile',self.manifest['aws_profile'],
                 '--region',self.manifest['region'],'--output','json',service,action]
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=60,env=aws_environment.environment(self.manifest['aws_profile']))
            if result.returncode:raise ValueError('AWS preflight request failed')
            value=json.loads(result.stdout)
            if not isinstance(value,dict):raise ValueError('Invalid AWS response')
            return value
        except (OSError,subprocess.TimeoutExpired,ValueError):
            raise ValueError('AWS read-only preflight failed; verify the selected profile and region') from None


def live_plan(aws,manifest,cidr,zones):
    """Read-only preflight; the caller supplies an account/region-scoped client."""
    plan=build_plan(manifest,cidr,zones)
    identity=aws.call('sts','get-caller-identity')
    arn=identity.get('Arn','')
    expected_role='em-'+manifest['group']+'-operator'
    if (identity.get('Account')!=manifest['account'] or not re.fullmatch(
            r'arn:aws:sts::'+manifest['account']+r':assumed-role/'
            +re.escape(expected_role)+r'/[A-Za-z0-9_+=,.@-]{2,64}',arn)):
        raise ValueError('Use the deployment group operator role through its scoped AWS profile')
    response=aws.call('ec2','describe-availability-zones')
    available=response.get('AvailabilityZones',[])
    selected=[zone for zone in available if zone.get('ZoneName') in zones]
    if response.get('NextToken') or len(selected)!=len(zones):
        raise ValueError('Availability Zone response is incomplete or ambiguous')
    zone_ids={zone['ZoneName']:zone.get('ZoneId','') for zone in selected}
    if (len(zone_ids)!=len(zones) or len(set(zone_ids.values()))!=len(zones)
            or not all(zone_ids.values())):
        raise ValueError('Availability Zone identities are incomplete or ambiguous')
    for zone in selected:
        if (zone.get('RegionName')!=manifest['region'] or zone.get('State')!='available'
                or zone.get('ZoneType')!='availability-zone'
                or zone.get('OptInStatus') not in ('opted-in','opt-in-not-required')):
            raise ValueError('A selected Availability Zone is not usable')
    response=aws.call('ec2','describe-vpcs')
    if response.get('NextToken'):
        raise ValueError('VPC inventory is incomplete')
    seen=set()
    network=ipaddress.IPv4Network(cidr)
    for existing in response['Vpcs']:
        identifier=existing['VpcId']
        if existing.get('OwnerId')!=manifest['account'] or identifier in seen:
            raise ValueError('VPC inventory has foreign or duplicate entries')
        seen.add(identifier)
        if any(tag.get('Key')=='enmaas:run' and tag.get('Value')==manifest['id']
               for tag in existing.get('Tags',[])):
            raise ValueError('Deployment already has a VPC; inspect its journal before continuing')
        cidrs=[existing['CidrBlock']]
        cidrs.extend(association['CidrBlock'] for association in existing.get('CidrBlockAssociationSet',[])
                     if association.get('CidrBlockState',{}).get('State')!='disassociated')
        if any(network.overlaps(ipaddress.IPv4Network(block)) for block in cidrs):
            raise ValueError('Requested network overlaps an existing regional VPC')
    plan.update(aws_validated=True,zone_ids=zone_ids,
                preflight_scope='Account, zones and visible regional VPC CIDRs only; not capacity, launch permissions or remote-network overlap')
    plan['qualification_pending']=plan['qualification_pending'][1:]
    return plan


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--cidr',required=True)
    parser.add_argument('--zone',action='append',required=True)
    parser.add_argument('--live',action='store_true',help='check account, zones and regional VPC overlap using the manifest profile; no resource changes')
    args=parser.parse_args()
    manifest=registry.load(args.manifest)
    plan=live_plan(Aws(manifest),manifest,args.cidr,args.zone) if args.live else build_plan(manifest,args.cidr,args.zone)
    print(json.dumps(plan,indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as error:raise SystemExit('VPC plan stopped: '+str(error)) from None
