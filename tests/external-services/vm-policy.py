#!/usr/bin/env python3
"""Render a group-scoped VM provisioning fragment; no IAM changes or AWS calls."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re

spec=importlib.util.spec_from_file_location('vm_network_policy',Path(__file__).with_name('network-policy.py'))
network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)


def vm_policy(manifest,images,instance_types,max_volume_gib):
    base,regional,requested,owned=network.scope(manifest)
    if (not images or any(not re.fullmatch(r'ami-[a-f0-9]{8,17}',value) for value in images)
            or not instance_types or any(not re.fullmatch(r'[a-z][a-z0-9-]+\.[a-z0-9]+',value) for value in instance_types)
            or not 8<=max_volume_gib<=16384):
        raise ValueError('Supply exact AMI IDs, instance types and a disk cap from 8 through 16384 GiB')
    run='em-'+manifest['group']+'-'+'?'*12
    names=[run+'-'+role for role in ('gateway','keycloak','observability')]
    requested['ForAllValues:StringEquals']['aws:TagKeys']+=['ManagedBy','ResourcePrefix','Scenario']
    requested['StringEquals'].update({'aws:RequestTag/ManagedBy':'secure-single-server-test',
                                     'aws:RequestTag/Scenario':['remote-gateway','keycloak','observability']})
    requested['StringLike'].update({'aws:RequestTag/ResourcePrefix':names,'aws:RequestTag/Name':names})
    statements=[]

    def add(sid,actions,resources,condition):
        statements.append({'Sid':sid,'Effect':'Allow','Action':['ec2:'+action for action in actions],
                           'Resource':resources,'Condition':copy.deepcopy(condition)})

    def resources(kind):
        return [base+kind+'/'+name for name in names] if kind=='key-pair' else [base+kind+'/*']

    add('VmDiscovery',['DescribeInstances','DescribeImages','DescribeInstanceTypes','DescribeInstanceTypeOfferings',
                       'DescribeKeyPairs','DescribeVolumes','DescribeSubnets','DescribeRouteTables','DescribeSecurityGroups'],
        ['*'],regional)
    add('ApprovedImages',['RunInstances'],['arn:aws:ec2:'+manifest['region']+'::image/'+image for image in sorted(set(images))],regional)
    for kind in ('subnet','security-group','key-pair'):
        add('LaunchOwned'+kind.replace('-','').title(),['RunInstances'],resources(kind),owned)
    instance=copy.deepcopy(requested)
    instance['StringEquals'].update({'ec2:InstanceType':sorted(set(instance_types)),
                                    'ec2:MetadataHttpTokens':'required','ec2:MetadataHttpEndpoint':'enabled'})
    instance['NumericEquals']={'ec2:MetadataHttpPutResponseHopLimit':'1'}
    instance['Null']['ec2:InstanceProfile']='true'
    add('LaunchInstances',['RunInstances'],resources('instance'),instance)
    volume=copy.deepcopy(requested)
    volume['Bool']={'ec2:Encrypted':'true'}
    volume['StringEquals']['ec2:VolumeType']='gp3'
    volume['NumericLessThanEquals']={'ec2:VolumeSize':str(max_volume_gib)}
    add('LaunchVolumes',['RunInstances'],resources('volume'),volume)
    add('LaunchInterfaces',['RunInstances'],resources('network-interface'),requested)
    for kind,action in (('security-group','CreateSecurityGroup'),('key-pair','ImportKeyPair')):
        add(action,[action],resources(kind),requested)
    add('CreateGroupInOwnedVpc',['CreateSecurityGroup'],resources('vpc'),owned)
    add('OwnedGroupIngress',['AuthorizeSecurityGroupIngress'],resources('security-group'),owned)
    for kind,action in (('instance','RunInstances'),('volume','RunInstances'),('network-interface','RunInstances'),
                        ('security-group','CreateSecurityGroup'),('key-pair','ImportKeyPair')):
        tagged=copy.deepcopy(requested);tagged['StringEquals']['ec2:CreateAction']=action
        add('Tag'+kind.replace('-','').title(),['CreateTags'],resources(kind),tagged)
    return {'Version':'2012-10-17','Statement':statements}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path)
    parser.add_argument('--image-id',action='append',required=True)
    parser.add_argument('--instance-type',action='append',required=True)
    parser.add_argument('--max-volume-gib',type=int,required=True)
    args=parser.parse_args()
    print(json.dumps(vm_policy(network.registry.load(args.manifest),args.image_id,args.instance_type,args.max_volume_gib),indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as error:raise SystemExit('VM policy stopped: '+str(error)) from None
