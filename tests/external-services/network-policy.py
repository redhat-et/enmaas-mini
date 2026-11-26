#!/usr/bin/env python3
"""Render the group-scoped network policy fragment; no IAM mutation or credentials."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path

spec=importlib.util.spec_from_file_location('network_policy_registry',Path(__file__).with_name('deployments.py'))
registry=importlib.util.module_from_spec(spec);spec.loader.exec_module(registry)
NEW={'vpc':'CreateVpc','subnet':'CreateSubnet','route-table':'CreateRouteTable','internet-gateway':'CreateInternetGateway'}
EXISTING={'CreateSubnet':['vpc'],'CreateRouteTable':['vpc'],
          'ModifyVpcAttribute':['vpc'],'ModifySubnetAttribute':['subnet'],
          'AttachInternetGateway':['internet-gateway','vpc'],
          'AssociateRouteTable':['route-table','subnet'],'CreateRoute':['route-table']}


def scope(manifest):
    registry.validate(manifest)
    if manifest['version']!=2:raise ValueError('Network policy requires a deployment group manifest')
    if manifest['region'].startswith(('cn-','us-gov-')):
        raise ValueError('This policy fragment supports the commercial ARN partition only')
    region=manifest['region'];group=manifest['group']
    base='arn:aws:ec2:'+region+':'+manifest['account']+':'
    run='em-'+group+'-'+'?'*12
    regional={'StringEquals':{'aws:RequestedRegion':region}}
    requested={'StringEquals':{'aws:RequestedRegion':region,'aws:RequestTag/enmaas:group':group,
                               'aws:RequestTag/enmaas:purpose':'external-services-test'},
               'StringLike':{'aws:RequestTag/enmaas:run':run},
               'ForAllValues:StringEquals':{'aws:TagKeys':['Name','enmaas:group','enmaas:run','enmaas:purpose']},
               'Null':{'aws:TagKeys':'false'}}
    owned={'StringEquals':{'aws:RequestedRegion':region,'aws:ResourceTag/enmaas:group':group,
                           'aws:ResourceTag/enmaas:purpose':'external-services-test'},
           'StringLike':{'aws:ResourceTag/enmaas:run':run}}
    return base,regional,requested,owned


def network_policy(manifest):
    base,regional,requested,owned=scope(manifest)
    statements=[{'Sid':'Identity','Effect':'Allow','Action':['sts:GetCallerIdentity'],'Resource':['*']},
                {'Sid':'NetworkDiscovery','Effect':'Allow','Action':[
                    'ec2:DescribeAvailabilityZones','ec2:DescribeVpcs','ec2:DescribeVpcAttribute',
                    'ec2:DescribeSubnets','ec2:DescribeRouteTables','ec2:DescribeInternetGateways',
                    'ec2:DescribeNetworkInterfaces','ec2:DescribeSecurityGroups'],
                 'Resource':['*'],'Condition':regional}]
    for kind,action in NEW.items():
        statements.append({'Sid':action,'Effect':'Allow','Action':['ec2:'+action],
                           'Resource':[base+kind+'/*'],'Condition':copy.deepcopy(requested)})
        condition=copy.deepcopy(requested)
        condition['StringEquals']['ec2:CreateAction']=action
        statements.append({'Sid':action+'Tags','Effect':'Allow','Action':['ec2:CreateTags'],
                           'Resource':[base+kind+'/*'],'Condition':condition})
    for action,kinds in EXISTING.items():
        statements.append({'Sid':action+'Owned','Effect':'Allow','Action':['ec2:'+action],
                           'Resource':[base+kind+'/*' for kind in kinds],'Condition':copy.deepcopy(owned)})
    return {'Version':'2012-10-17','Statement':statements}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path)
    args=parser.parse_args()
    print(json.dumps(network_policy(registry.load(args.manifest)),indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as error:raise SystemExit('Network policy stopped: '+str(error)) from None
