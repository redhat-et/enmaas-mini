#!/usr/bin/env python3
"""Plan or explicitly grant three narrowly scoped lab peer ingress rules."""
import argparse
import importlib.machinery
import importlib.util
import ipaddress
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
ROLES=('gateway','keycloak','observability')
loader=importlib.machinery.SourceFileLoader('lab_vm',str(ROOT/'scripts/aws/rhel-vm'))
spec=importlib.util.spec_from_loader(loader.name,loader)
vm=importlib.util.module_from_spec(spec);loader.exec_module(vm)


def peer_rules(hosts,issuer_path='public'):
    if issuer_path not in ('public','private'):
        raise ValueError('Issuer path must be public or private')
    if set(hosts)!=set(ROLES) or len({h.get('VpcId') for h in hosts.values()})!=1:
        raise ValueError('All three hosts must belong to the same VPC')
    public=[]
    private=[]
    for host in hosts.values():
        try:
            public.append(str(ipaddress.IPv4Address(host['PublicIpAddress'])))
            private.append(str(ipaddress.IPv4Address(host['PrivateIpAddress'])))
        except (KeyError,ValueError):
            raise ValueError('Each running host needs public and private IPv4 addresses') from None
    if len(set(public))!=3 or len(set(private))!=3:
        raise ValueError('Each role must use a separate host')
    if issuer_path=='private':
        networks=tuple(ipaddress.IPv4Network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
        if any(not any(ipaddress.IPv4Address(address) in n for n in networks) for address in private):
            raise ValueError('Private issuer routing requires RFC1918 host addresses')
    result=[]
    # The DNS issuer may resolve privately inside clients; never change its URL.
    # SG references cannot authorize the legacy public-IP path.
    for source in ('gateway','observability'):
        origin=({'UserIdGroupPairs':[{'GroupId':hosts[source]['SecurityGroups'][0]['GroupId']}]}
                if issuer_path=='private' else {'IpRanges':[{'CidrIp':hosts[source]['PublicIpAddress']+'/32'}]})
        result.append({'target':'keycloak','source':source,'permission':{
            'IpProtocol':'tcp','FromPort':8443,'ToPort':8443,
            **origin}})
    result.append({'target':'observability','source':'gateway','permission':{
        'IpProtocol':'tcp','FromPort':24443,'ToPort':24443,
        'UserIdGroupPairs':[{'GroupId':hosts['gateway']['SecurityGroups'][0]['GroupId']}]}})
    return result


def ruleset(permissions):
    result=set()
    for rule in permissions:
        prefix=(rule.get('IpProtocol'),rule.get('FromPort'),rule.get('ToPort'))
        for field,key,kind in (('IpRanges','CidrIp','ipv4'),('Ipv6Ranges','CidrIpv6','ipv6'),
                               ('UserIdGroupPairs','GroupId','group'),('PrefixListIds','PrefixListId','prefix')):
            for entry in rule.get(field,[]):result.add((*prefix,kind,entry[key]))
        if not any(rule.get(f) for f in ('IpRanges','Ipv6Ranges','UserIdGroupPairs','PrefixListIds')):
            result.add((*prefix,'unknown',''))
    return result


def inspect(aws,args):
    caller=aws.call('sts','get-caller-identity')
    if caller['Account']!=args.account_id:raise ValueError('AWS caller account differs from explicit target')
    states={};hosts={};groups={}
    for role in ROLES:
        scenario='remote-gateway' if role=='gateway' else role
        states[role]=vm.launch_state(args.state_dir/(args.prefix+'-'+role+'.json'),args,scenario)
        state=states[role]
        if state['Prefix']!=args.prefix+'-'+role:raise ValueError('Journal run prefix differs')
        hosts[role]=vm.described_instance(aws,state,scenario)
        response=aws.call('ec2','describe-security-groups',group_ids=[state['SecurityGroupId']])
        if len(response['SecurityGroups'])!=1:raise ValueError('Security group missing')
        group=response['SecurityGroups'][0]
        if (group.get('OwnerId')!=args.account_id or group.get('VpcId')!=hosts[role]['VpcId']
                or not vm.owned_tags(group,state['Prefix'],scenario)):
            raise ValueError('Security group ownership or VPC differs')
        groups[role]=group
    rules=peer_rules(hosts,getattr(args,'issuer_path','public'))
    missing=[]
    for role in ROLES:
        expected=ruleset(states[role]['Ingress'])
        for protocol,start,end,kind,source in expected:
            if (protocol!='tcp' or start not in (22,8443) or end!=start or kind!='ipv4'
                    or ipaddress.IPv4Network(source).prefixlen!=32):
                raise ValueError('Lab journals must contain only restricted workstation SSH/HTTPS ingress')
        peers=ruleset([r['permission'] for r in rules if r['target']==role])
        actual=ruleset(groups[role].get('IpPermissions',[]))
        if actual-(expected|peers):raise ValueError('Unexpected ingress exists; inspect security groups before granting peers')
        if not expected<=actual:raise ValueError('Original restricted ingress is missing; inspect the security group')
        for rule in rules:
            if rule['target']==role and not ruleset([rule['permission']])<=actual:
                missing.append({'group_id':states[role]['SecurityGroupId'],**rule})
    return hosts,missing


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-id',required=True)
    parser.add_argument('--region',required=True)
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--profile')
    parser.add_argument('--state-dir',type=Path,default=ROOT/'.state')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--issuer-path',choices=('public','private'),default='public',
        help='private requires a DNS issuer, matching certificate and private mappings in both clients')
    args=parser.parse_args()
    if not re.fullmatch(r'[0-9]{12}',args.account_id):parser.error('Expected explicit 12-digit account ID')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{1,60}',args.prefix):parser.error('Invalid run prefix')
    aws=vm.Aws(args.region,args.profile,args.apply)
    hosts,missing=inspect(aws,args)
    print(json.dumps({'AccountId':args.account_id,'Region':args.region,'Prefix':args.prefix,
        'IssuerPath':args.issuer_path,
        'IssuerAddress':hosts['keycloak']['PrivateIpAddress' if args.issuer_path=='private' else 'PublicIpAddress']+':8443',
        'MissingIngress':missing,'TelemetryAddress':hosts['observability']['PrivateIpAddress']+':24443'},indent=2))
    if not args.apply or not missing:return
    confirmation=f'grant peers {args.account_id} {args.region} {args.prefix}'
    if input(f"Type exactly '{confirmation}': ")!=confirmation:raise ValueError('Confirmation did not match')
    # Re-read account, ownership and current addresses after human review.
    current_hosts,current_missing=inspect(aws,args)
    if current_hosts!=hosts or current_missing!=missing:raise ValueError('Resources changed after review; rerun plan')
    for rule in missing:
        try:
            aws.call('ec2','authorize-security-group-ingress',group_id=rule['group_id'],
                     ip_permissions=[rule['permission']])
        except vm.AwsError as error:
            if error.code!='InvalidPermission.Duplicate':raise
    _,remaining=inspect(aws,args)
    if remaining:raise ValueError('Some peer rules are missing; inspect and rerun')
    print('Peer ingress verified. No SSH, database or raw query ports were added.')

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError) as error:raise SystemExit('Network setup stopped: '+str(error))
