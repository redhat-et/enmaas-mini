#!/usr/bin/env python3
"""Plan, explicitly create and verify a private RDS test database."""
import argparse
import json
import os
import importlib.util
import subprocess
import tempfile
from pathlib import Path
import re


def build_plan(*,account,region,prefix,vpc,subnets,gateway_group,engine_version,instance_class):
    if not re.fullmatch(r'[0-9]{12}',account):raise ValueError('Explicit AWS account required')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]',region):raise ValueError('Invalid region')
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,40}',prefix) or prefix.endswith('-') or '--' in prefix:
        raise ValueError('Use a lowercase run prefix without trailing or repeated hyphens')
    if not re.fullmatch(r'vpc-[a-f0-9]{8,17}',vpc):raise ValueError('Invalid VPC ID')
    if not re.fullmatch(r'sg-[a-f0-9]{8,17}',gateway_group):raise ValueError('Invalid gateway security group')
    if not re.fullmatch(r'(16|17|18)\.[0-9]+(?:\.[0-9]+)?',engine_version):
        raise ValueError('Select an explicit supported PostgreSQL 16–18 minor version')
    if instance_class not in ('db.t4g.small','db.t4g.medium'):
        raise ValueError('Testing profile requires db.t4g.small or db.t4g.medium')
    if not 2<=len(subnets)<=20 or len({s['SubnetId'] for s in subnets})!=len(subnets):raise ValueError('Two distinct subnets required')
    if len({s['AvailabilityZone'] for s in subnets})<2:raise ValueError('RDS requires at least two Availability Zones')
    for subnet in subnets:
        if subnet['VpcId']!=vpc or subnet.get('State')!='available' or subnet.get('AvailableIpAddressCount',0)<8:
            raise ValueError('Subnets must be available in the target VPC with free addresses')
        if not re.fullmatch(r'subnet-[a-f0-9]{8,17}',subnet['SubnetId']):raise ValueError('Invalid subnet ID')
    name=prefix+'-postgres'
    tags=[{'Key':'enmaas:run','Value':prefix},{'Key':'enmaas:purpose','Value':'external-services-test'}]
    group = re.fullmatch(r'em-([a-z][a-z0-9]{0,7})-[a-f0-9]{12}', prefix)
    if group:
        tags.append({'Key':'enmaas:group','Value':group[1]})
    return {'AccountId':account,'Region':region,'VpcId':vpc,'Prefix':prefix,
        'SubnetGroup':{'DBSubnetGroupName':name,'DBSubnetGroupDescription':'enMAAS private test PostgreSQL',
                       'SubnetIds':[s['SubnetId'] for s in subnets],'Tags':tags},
        'SecurityGroup':{'GroupName':name,'Description':'Gateway-only PostgreSQL test service','VpcId':vpc},
        'Ingress':{'IpProtocol':'tcp','FromPort':5432,'ToPort':5432,
                   'UserIdGroupPairs':[{'GroupId':gateway_group,'UserId':account}]},
        'ParameterGroup':{'DBParameterGroupName':name,'DBParameterGroupFamily':'postgres'+engine_version.split('.')[0],
                          'Description':'Require TLS for lab PostgreSQL','Tags':tags},
        'Parameters':[{'ParameterName':'rds.force_ssl','ParameterValue':'1','ApplyMethod':'pending-reboot'}],
        'Instance':{'DBInstanceIdentifier':name,'Engine':'postgres','EngineVersion':engine_version,
            'DBInstanceClass':instance_class,'AllocatedStorage':20,'MaxAllocatedStorage':40,'StorageType':'gp3',
            'StorageEncrypted':True,'PubliclyAccessible':False,'MultiAZ':False,'NetworkType':'IPV4',
            'MasterUsername':'labadmin','ManageMasterUserPassword':True,'DBSubnetGroupName':name,
            'DBParameterGroupName':name,'Port':5432,'BackupRetentionPeriod':1,'DeletionProtection':True,
            'CopyTagsToSnapshot':True,'AutoMinorVersionUpgrade':True,'Tags':tags},
        'AcceptancePending':['Live account, subnet, gateway ownership and orderable-version validation',
            'Confirmed apply with per-resource recovery journal and verification',
            'TLS verify-full, least-privilege application roles and failure/recovery test'],
        'Warning':'Offline plan only. Security group ID must be resolved after creation; do not submit Instance alone.'}


class ReadOnlyAws:
    allowed={('sts','get-caller-identity'),('ec2','describe-instances'),('ec2','describe-security-groups'),
             ('ec2','describe-subnets'),('ec2','describe-vpc-attribute'),
             ('rds','describe-db-engine-versions'),('rds','describe-orderable-db-instance-options'),
             ('rds','describe-db-instances'),('rds','describe-db-parameters'),
             ('rds','describe-db-parameter-groups'),('rds','list-tags-for-resource'),
             ('rds','describe-db-snapshots'),('rds','describe-db-instance-automated-backups')}

    def __init__(self,region,profile=None):self.region=region;self.profile=profile

    def call(self,service,action,**options):
        if (service,action) not in self.allowed:raise ValueError('AWS operation refused by helper allowlist')
        args=['aws','--no-cli-pager','--region',self.region,'--output','json']
        if self.profile:args.extend(['--profile',self.profile])
        payload={''.join(word[:1].upper()+word[1:] for word in key.split('_')):value for key,value in options.items()}
        # EC2/RDS API acronym spelling differs from generic title casing.
        for old,new in [('DbInstanceClass','DBInstanceClass'),('DbInstanceIdentifier','DBInstanceIdentifier'),('DbParameterGroupName','DBParameterGroupName')]:
            if old in payload and new!=old:payload[new]=payload.pop(old)
        args.extend([service,action,'--cli-input-json',json.dumps(payload)])
        result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=60)
        if result.returncode:raise ValueError('AWS read failed: '+service+' '+action)
        return json.loads(result.stdout)


def live_plan(aws,args):
    # Reuse the VM journal and live ownership checks, not untrusted IDs alone.
    spec=importlib.util.spec_from_file_location('lab_network',Path(__file__).with_name('network.py'))
    network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)
    if aws.call('sts','get-caller-identity')['Account']!=args.account_id:
        raise ValueError('AWS account differs from explicit target')
    state=network.vm.launch_state(args.gateway_journal,args,'remote-gateway')
    if state['Prefix']!=args.prefix+'-gateway':raise ValueError('Gateway journal belongs to another lab run')
    host=network.vm.described_instance(aws,state,'remote-gateway')
    group=aws.call('ec2','describe-security-groups',group_ids=[state['SecurityGroupId']])['SecurityGroups']
    if (len(group)!=1 or group[0].get('OwnerId')!=args.account_id or group[0].get('VpcId')!=host['VpcId']
            or not network.vm.owned_tags(group[0],state['Prefix'],'remote-gateway')):
        raise ValueError('Gateway security group ownership differs')
    subnets=aws.call('ec2','describe-subnets',subnet_ids=args.subnet_ids)['Subnets']
    if set(s['SubnetId'] for s in subnets)!=set(args.subnet_ids):raise ValueError('Subnet lookup differs')
    for attribute,key in [('enableDnsSupport','EnableDnsSupport'),('enableDnsHostnames','EnableDnsHostnames')]:
        if not aws.call('ec2','describe-vpc-attribute',vpc_id=host['VpcId'],attribute=attribute)[key]['Value']:
            raise ValueError('RDS lab requires VPC DNS support and hostnames')
    versions=aws.call('rds','describe-db-engine-versions',engine='postgres',engine_version=args.engine_version)['DBEngineVersions']
    family='postgres'+args.engine_version.split('.')[0]
    if not any(v.get('EngineVersion')==args.engine_version and v.get('DBParameterGroupFamily')==family for v in versions):
        raise ValueError('Requested PostgreSQL version/family is unavailable')
    choices=aws.call('rds','describe-orderable-db-instance-options',engine='postgres',engine_version=args.engine_version,
                     db_instance_class=args.instance_class,vpc=True)['OrderableDBInstanceOptions']
    if not any(c.get('StorageType')=='gp3' and c.get('SupportsStorageEncryption') for c in choices):
        raise ValueError('Requested encrypted gp3 class/version is not orderable')
    plan=build_plan(account=args.account_id,region=args.region,prefix=args.prefix,vpc=host['VpcId'],
        subnets=subnets,gateway_group=state['SecurityGroupId'],engine_version=args.engine_version,instance_class=args.instance_class)
    plan['GatewayInstanceId']=host['InstanceId']
    plan['AcceptancePending']=plan['AcceptancePending'][1:]
    plan['Warning']='Read-only live plan. No resources created. Creation requires --apply and a new recovery journal.'
    return plan


class ProvisioningAws(ReadOnlyAws):
    allowed=ReadOnlyAws.allowed|{('ec2','create-security-group'),('ec2','authorize-security-group-ingress'),
        ('rds','create-db-subnet-group'),('rds','create-db-parameter-group'),
        ('rds','modify-db-parameter-group'),('rds','create-db-instance')}


def create(aws,plan,path):
    # No resume or adoption: ambiguous results must be reconciled manually.
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    state={'Plan':plan,'Resources':{},'Pending':None,'Created':False}
    def save():
        temporary=None
        try:
            temporary_fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
            with os.fdopen(temporary_fd,'w') as stream:
                json.dump(state,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            parent_fd=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(parent_fd)
            finally:os.close(parent_fd)
        finally:
            if temporary and os.path.exists(temporary):os.unlink(temporary)
    def operation(name,service,action,options,extract):
        state['Pending']={'Name':name,'Service':service,'Action':action};save()
        response=aws.call(service,action,**options)
        state['Resources'][name]=extract(response)
        state['Pending']=None;save()
    try:
        save()
        tags=plan['Instance']['Tags']
        operation('SecurityGroup','ec2','create-security-group',{
            **plan['SecurityGroup'],'TagSpecifications':[{'ResourceType':'security-group','Tags':tags}]},lambda r:r['GroupId'])
        group=state['Resources']['SecurityGroup']
        operation('Ingress','ec2','authorize-security-group-ingress',{'GroupId':group,'IpPermissions':[plan['Ingress']]},lambda r:True)
        operation('SubnetGroup','rds','create-db-subnet-group',plan['SubnetGroup'],lambda r:r['DBSubnetGroup']['DBSubnetGroupArn'])
        operation('ParameterGroup','rds','create-db-parameter-group',plan['ParameterGroup'],lambda r:r['DBParameterGroup']['DBParameterGroupArn'])
        operation('TLSParameter','rds','modify-db-parameter-group',{
            'DBParameterGroupName':plan['ParameterGroup']['DBParameterGroupName'],'Parameters':plan['Parameters']},lambda r:True)
        operation('Instance','rds','create-db-instance',{
            **plan['Instance'],'VpcSecurityGroupIds':[group]},lambda r:{
                'Identifier':r['DBInstance']['DBInstanceIdentifier'],'Arn':r['DBInstance']['DBInstanceArn'],
                'ResourceId':r['DBInstance']['DbiResourceId'],
                'MasterSecretArn':r['DBInstance'].get('MasterUserSecret',{}).get('SecretArn')})
        state['Created']=True;save()
        return state
    finally:os.close(fd)


def verify(aws,state,account,region):
    plan=state['Plan']
    if plan['AccountId']!=account or plan['Region']!=region or aws.call('sts','get-caller-identity')['Account']!=account:
        raise ValueError('Journal/account/region differ')
    if not state['Created'] or state['Pending']:raise ValueError('Creation incomplete; inspect journal and tagged resources')
    result=aws.call('rds','describe-db-instances',db_instance_identifier=plan['Instance']['DBInstanceIdentifier'])['DBInstances']
    if len(result)!=1:raise ValueError('RDS instance missing')
    db=result[0];expected=plan['Instance']
    if db['DBInstanceArn']!=state['Resources']['Instance']['Arn']:raise ValueError('RDS resource identity differs')
    resource_id=state['Resources']['Instance'].get('ResourceId')
    if not resource_id or db.get('DbiResourceId')!=resource_id:
        raise ValueError('RDS immutable resource ID missing or changed; manual journal reconciliation required')
    for field in ('Engine','EngineVersion','DBInstanceClass','StorageEncrypted','PubliclyAccessible','DeletionProtection','BackupRetentionPeriod','MultiAZ','AutoMinorVersionUpgrade'):
        if db.get(field)!=expected[field]:raise ValueError('RDS setting differs: '+field)
    if (db.get('StorageType')!=expected['StorageType'] or db.get('MaxAllocatedStorage')!=expected['MaxAllocatedStorage']
            or not expected['AllocatedStorage']<=db.get('AllocatedStorage',0)<=expected['MaxAllocatedStorage']):
        raise ValueError('RDS storage bounds differ')
    if db.get('Endpoint') and db['Endpoint'].get('Port')!=5432:raise ValueError('RDS port differs')
    group=state['Resources']['SecurityGroup']
    if ([g['VpcSecurityGroupId'] for g in db['VpcSecurityGroups']]!=[group]
            or db['DBSubnetGroup']['VpcId']!=plan['VpcId']
            or db['DBSubnetGroup']['DBSubnetGroupName']!=expected['DBSubnetGroupName']):
        raise ValueError('RDS network differs')
    groups=aws.call('ec2','describe-security-groups',group_ids=[group])['SecurityGroups']
    if len(groups)!=1 or groups[0]['OwnerId']!=account or groups[0]['VpcId']!=plan['VpcId']:
        raise ValueError('RDS security-group ownership differs')
    # Normalize AWS-added metadata but reject every extra CIDR, peer or port.
    spec=importlib.util.spec_from_file_location('rds_network_verify',Path(__file__).with_name('network.py'))
    network=importlib.util.module_from_spec(spec);spec.loader.exec_module(network)
    if network.ruleset(groups[0].get('IpPermissions',[]))!=network.ruleset([plan['Ingress']]):
        raise ValueError('RDS ingress differs from gateway-only policy')
    expected_tags={tag['Key']:tag['Value'] for tag in expected['Tags']}
    group_tags={tag['Key']:tag['Value'] for tag in groups[0].get('Tags',[])}
    if any(group_tags.get(key)!=value for key,value in expected_tags.items()):
        raise ValueError('RDS security-group ownership tag differs')
    secret=db.get('MasterUserSecret',{}).get('SecretArn')
    if not secret or secret!=state['Resources']['Instance'].get('MasterSecretArn'):
        raise ValueError('Managed master credential identity differs from the journal')
    if db['DBSubnetGroup'].get('DBSubnetGroupArn')!=state['Resources'].get('SubnetGroup'):
        raise ValueError('Subnet group identity differs from the journal')
    parameters=db.get('DBParameterGroups',[])
    if len(parameters)!=1 or parameters[0]['DBParameterGroupName']!=expected['DBParameterGroupName']:
        raise ValueError('RDS parameter group differs')
    values=aws.call('rds','describe-db-parameters',db_parameter_group_name=expected['DBParameterGroupName'])['Parameters']
    if not any(p['ParameterName']=='rds.force_ssl' and p.get('ParameterValue')=='1' for p in values):
        raise ValueError('RDS TLS enforcement differs')
    parameter_groups=aws.call('rds','describe-db-parameter-groups',
                              db_parameter_group_name=expected['DBParameterGroupName'])['DBParameterGroups']
    if (len(parameter_groups)!=1 or not state['Resources'].get('ParameterGroup')
            or parameter_groups[0].get('DBParameterGroupArn')!=state['Resources']['ParameterGroup']):
        raise ValueError('Parameter group identity differs from the journal')
    for arn in (db['DBInstanceArn'],state['Resources']['SubnetGroup'],state['Resources']['ParameterGroup']):
        actual={tag['Key']:tag['Value'] for tag in aws.call('rds','list-tags-for-resource',resource_name=arn)['TagList']}
        if any(actual.get(key)!=value for key,value in expected_tags.items()):
            raise ValueError('RDS dependency ownership tags differ')
    ready=(db['DBInstanceStatus']=='available' and parameters[0]['ParameterApplyStatus']=='in-sync'
           and db['MasterUserSecret'].get('SecretStatus')=='active')
    return {'Ready':ready,'State':db['DBInstanceStatus'],'Endpoint':db.get('Endpoint',{}).get('Address'),
            'TLSMode':'verify-full','ApplicationRolesAndConnectivity':'not qualified'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Confirm billable resource creation')
    parser.add_argument('--journal',type=Path,help='Exclusive creation/recovery journal path')
    parser.add_argument('--verify',type=Path,help='Read-only verification of a completed creation journal')
    parser.add_argument('--input',type=Path,help='Offline non-secret JSON inputs')
    parser.add_argument('--gateway-journal',type=Path,help='Enable read-only AWS validation from a gateway launch journal')
    parser.add_argument('--account-id');parser.add_argument('--region');parser.add_argument('--profile')
    parser.add_argument('--prefix');parser.add_argument('--engine-version')
    parser.add_argument('--instance-class',default='db.t4g.small')
    parser.add_argument('--subnet-id',dest='subnet_ids',action='append')
    args=parser.parse_args()
    if args.verify:
        if args.apply or args.input or args.gateway_journal or not args.account_id or not args.region:
            parser.error('Verify needs only journal, explicit account/region and optional profile')
        print(json.dumps(verify(ReadOnlyAws(args.region,args.profile),json.loads(args.verify.read_text()),args.account_id,args.region),indent=2));return
    if args.apply and (not args.gateway_journal or not args.journal):parser.error('Apply requires a live gateway journal and new recovery journal')
    if bool(args.input)==bool(args.gateway_journal):parser.error('Select offline input or a gateway journal')
    if args.input:plan=build_plan(**json.loads(args.input.read_text()))
    else:
        if not all((args.account_id,args.region,args.prefix,args.engine_version,args.subnet_ids)):
            parser.error('Live planning requires account, region, prefix, engine version and subnet IDs')
        plan=live_plan(ReadOnlyAws(args.region,args.profile),args)
    print(json.dumps(plan,indent=2))
    if args.apply:
        confirmation=f'create RDS {args.account_id} {args.region} {args.prefix}'
        if input(f"Billable database launch. Type exactly '{confirmation}': ")!=confirmation:
            raise ValueError('Confirmation did not match')
        current=live_plan(ReadOnlyAws(args.region,args.profile),args)
        if current!=plan:raise ValueError('Plan changed after confirmation; rerun')
        create(ProvisioningAws(args.region,args.profile),plan,args.journal)
        print('Creation submitted; use --verify after provisioning. Recovery journal: '+str(args.journal))

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError,subprocess.SubprocessError):raise SystemExit('RDS operation failed; inspect the recovery journal and tagged resources before retrying. Nothing was automatically deleted') from None
