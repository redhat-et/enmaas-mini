#!/usr/bin/env python3
"""Secret-free host summary and explicitly approved private-peer handoff.

Checks running identities and ingress, not host SSH trust or application readiness.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('info_network',HERE/'network.py')
peers=importlib.util.module_from_spec(spec);spec.loader.exec_module(peers)
spec=importlib.util.spec_from_file_location('info_journal',HERE/'vpc-inspect.py')
journals=importlib.util.module_from_spec(spec);spec.loader.exec_module(journals)
registry=journals.registry
spec=importlib.util.spec_from_file_location('info_services',HERE/'services.py')
services=importlib.util.module_from_spec(spec);spec.loader.exec_module(services)
spec=importlib.util.spec_from_file_location('info_dns',HERE/'dns.py')
dns=importlib.util.module_from_spec(spec);spec.loader.exec_module(dns)


def summary(manifest,journal,aws):
    registry.validate(manifest)
    recorded=journals.inspect(journal/'network',manifest)
    if recorded['state']!='creation-recorded-complete':
        raise ValueError('Network creation is incomplete; inspect recovery records first')
    vpcs=[r['id'] for r in recorded['confirmed_resources'] if r['action']=='create-vpc']
    if len(vpcs)!=1:raise ValueError('Expected exactly one journaled VPC')
    for role in peers.ROLES:
        state=registry.read_private_json(journal/(manifest['id']+'-'+role+'.json'))
        if state.get('VpcId')!=vpcs[0]:raise ValueError('Host journal belongs to another VPC')
    args=argparse.Namespace(account_id=manifest['account'],region=manifest['region'],
                           prefix=manifest['id'],state_dir=journal,issuer_path='private')
    hosts,missing=peers.inspect(aws,args)
    rows={}
    for role,host in hosts.items():
        if host['VpcId']!=vpcs[0]:raise ValueError('Live host belongs to another VPC')
        address=host['PublicIpAddress']
        rows[role]={'instance':host['InstanceId'],'public_ip':address,'private_ip':host['PrivateIpAddress'],
            'ssh_login':'ec2-user@'+address,'ssh_key_path':manifest['ssh_key_path'],
            'journal':str(journal/(manifest['id']+'-'+role+'.json')),
            'https_base_url':'https://'+address+':8443'}
    return {'deployment':manifest['id'],'group':manifest.get('group'),'account':manifest['account'],
            'region':manifest['region'],'profile':manifest['aws_profile'],'vpc':vpcs[0],
            'hosts':rows,'missing_private_peer_rules':missing,
            'services_verified':False,'ssh_host_keys_verified':False,
            'notice':'Addresses are AWS-verified running hosts, not ready application URLs. Configure private issuer identity, TLS and services before use.'}


def prepare_dns(manifest,journal,aws,path,apply,confirmation):
    """Use AWS-verified host addresses with the existing DNS ownership protocol."""
    reviewed=summary(manifest,journal,aws)
    if apply and summary(manifest,journal,aws)!=reviewed:
        raise ValueError('Hosts changed before DNS preparation')
    args=argparse.Namespace(action='create' if apply else 'plan',journal=path,confirm=confirmation,
        **{role+'_ip':host['public_ip'] for role,host in reviewed['hosts'].items()})
    return dns.execute(args,manifest)


def configure_peers(manifest, journal, aws):
    """Grant only missing private peers after two complete ownership inspections.

    No launch/recovery operation is replayed. After an uncertain AWS write, a new
    invocation inspects the live rules and grants only those still absent.
    """
    reviewed = summary(manifest, journal, aws)
    if not reviewed['missing_private_peer_rules']:
        return reviewed
    if summary(manifest, journal, aws) != reviewed:
        raise ValueError('Resources changed after inspection; review again')
    for rule in reviewed['missing_private_peer_rules']:
        try:
            aws.call('ec2', 'authorize-security-group-ingress', group_id=rule['group_id'],
                     ip_permissions=[rule['permission']])
        except peers.vm.AwsError as error:
            if error.code != 'InvalidPermission.Duplicate':
                raise
    verified = summary(manifest, journal, aws)
    if (verified['missing_private_peer_rules'] or verified['hosts'] != reviewed['hosts']
            or verified['vpc'] != reviewed['vpc']):
        raise ValueError('Peer handoff incomplete or hosts changed; inspect live rules')
    return verified


def service_configuration(manifest,journal,aws,*,issuer_name,known_hosts,tls_directory,revision,source_repository,external_database=False,population_count=10000):
    live=summary(manifest,journal,aws)
    if live['missing_private_peer_rules']:
        raise ValueError('Apply and verify private peer rules before preparing service configuration')
    cidrs=set()
    for role in peers.ROLES:
        recorded=registry.read_private_json(journal/(manifest['id']+'-'+role+'.json'))
        rules=peers.ruleset(recorded['Ingress'])
        sources={source for protocol,start,end,kind,source in rules}
        if len(sources)!=1:raise ValueError('Each host must have one explicit workstation CIDR')
        source=next(iter(sources))
        expected={('tcp',port,port,'ipv4',source) for port in (22,8443)}
        if rules!=expected:raise ValueError('Expected exact restricted workstation SSH and HTTPS rules')
        cidrs.add(source)
    if len(cidrs)!=1:raise ValueError('Workstation source differs between service hosts')
    config={'version':1,'deployment':manifest['id'],'revision':revision,
            'repository':'/opt/enmaas-mini/'+manifest['id'],'source_repository':source_repository,
            'ssh_key':manifest['ssh_key_path'],'known_hosts':str(known_hosts),'tls_directory':str(tls_directory),
            'issuer_name':issuer_name,'workstation_cidr':next(iter(cidrs)),
            'external_database':external_database,'population_count':population_count,
            'hosts':{role:{'ssh':host['ssh_login'],'private_ip':host['private_ip']} for role,host in live['hosts'].items()}}
    return services.validate(config)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--journal',type=Path,required=True)
    parser.add_argument('--dns-journal',type=Path,help='preview DNS records for the verified hosts using this ownership journal')
    parser.add_argument('--apply-dns',action='store_true',help='create missing DNS records; never overwrite conflicts')
    parser.add_argument('--confirm-dns',help='exact: create DEPLOYMENT ACCOUNT')
    parser.add_argument('--apply-peers',action='store_true',help='grant only missing private peer ingress after ownership checks')
    parser.add_argument('--confirm',help='exact: grant peers ACCOUNT REGION DEPLOYMENT')
    parser.add_argument('--service-config',type=Path,help='write a private service config after live ownership and peer checks')
    parser.add_argument('--issuer-name')
    parser.add_argument('--known-hosts',type=Path)
    parser.add_argument('--tls-directory',type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--source-repository')
    parser.add_argument('--external-database',action='store_true')
    parser.add_argument('--population-count',type=int)
    args=parser.parse_args();manifest=registry.load(args.manifest)
    expected='grant peers '+manifest['account']+' '+manifest['region']+' '+manifest['id']
    if (args.apply_peers and args.confirm != expected) or (not args.apply_peers and args.confirm is not None):
        raise ValueError('Peer changes require --apply-peers and exact account/region/deployment confirmation')
    if args.dns_journal and not manifest['zone_id']:raise ValueError('DNS handoff needs a configured hosted zone')
    if (args.apply_dns and (not args.dns_journal or args.confirm_dns!='create '+manifest['id']+' '+manifest['account'])) or (not args.apply_dns and args.confirm_dns is not None):
        raise ValueError('DNS creation requires an ownership journal and exact deployment/account confirmation')
    options=(args.issuer_name,args.known_hosts,args.tls_directory,args.revision,args.source_repository)
    if args.service_config:
        if not all(options):raise ValueError('Service config requires issuer, trust paths, revision and source repository')
    elif any(options) or args.external_database or args.population_count is not None:raise ValueError('Service options require --service-config')
    aws=peers.vm.Aws(manifest['region'],manifest['aws_profile'],args.apply_peers)
    if args.apply_peers:configure_peers(manifest,args.journal,aws)
    dns_result=prepare_dns(manifest,args.journal,aws,args.dns_journal,args.apply_dns,args.confirm_dns) if args.dns_journal else None
    if args.service_config:
        config=service_configuration(manifest,args.journal,aws,issuer_name=args.issuer_name,known_hosts=args.known_hosts,
            tls_directory=args.tls_directory,revision=args.revision,source_repository=args.source_repository,external_database=args.external_database,
            population_count=10000 if args.population_count is None else args.population_count)
        directory=services.state.secure_dir(args.service_config.parent);os.close(directory)
        services.state.save(args.service_config.parent,args.service_config.name,config,exclusive=True)
        print(json.dumps({'deployment':manifest['id'],'service_config':str(args.service_config),
                         'services_installed':False,'issuer_dns_verified':False,'ssh_host_keys_verified':False,'dns':dns_result},indent=2))
    else:
        result=summary(manifest,args.journal,aws)
        if dns_result is not None:result['dns']=dns_result
        print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        raise SystemExit('Infrastructure inspection stopped: incomplete or inconsistent deployment; inspect private journals and live resources') from None
