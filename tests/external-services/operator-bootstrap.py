#!/usr/bin/env python3
"""Preview or explicitly create a network/VM operator role; no access keys are created."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import argparse
import os
import subprocess


def module(name,filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


vm=module('bootstrap_vm','vm-policy.py')
packing=module('bootstrap_pack','policy-pack.py')
network_create=module('bootstrap_journal','vpc-create.py')


class AwsFailure(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__('AWS bootstrap request failed: '+code)


class Aws:
    ALLOWED={('sts','get-caller-identity'),('iam','get-account-summary'),('iam','get-role'),
             ('iam','get-policy'),('iam','create-role'),('iam','create-policy'),('iam','attach-role-policy'),
             ('accessanalyzer','validate-policy')}

    def __init__(self,profile,region):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',profile):
            raise ValueError('Select an explicit administrator profile')
        self.profile=profile;self.region=region

    def call(self,service,action,**options):
        if (service,action) not in self.ALLOWED:raise ValueError('Bootstrap AWS action not allowed')
        command=['aws','--no-cli-pager','--profile',self.profile,'--region',self.region,
                 '--output','json',service,action,'--cli-input-json',json.dumps(options)]
        environment=network_create.planner.aws_environment.environment(self.profile)
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=60,env=environment)
            if result.returncode:
                match=re.search(r'\((NoSuchEntity|AccessDenied|EntityAlreadyExists|LimitExceeded|RequestTimeout)\)',result.stderr)
                raise AwsFailure(match[1] if match else 'RequestFailed')
            value=json.loads(result.stdout) if result.stdout.strip() else {}
            if not isinstance(value,dict):raise ValueError('Unexpected response')
            return value
        except AwsFailure:raise
        except (OSError,ValueError,subprocess.TimeoutExpired):raise AwsFailure('InvalidResponseOrTimeout') from None


def dns_policy(manifest, zone, domain):
    """Optional group namespace in one approved public zone; no zone or ACME grants."""
    registry=vm.network.registry
    registry.validate(dict(manifest,zone_id=zone,domain=domain))
    if not zone or not domain or manifest.get('version')!=2:
        raise ValueError('DNS grants require an explicit zone, domain and deployment group')
    suffix='em-'+manifest['group']+'-'+'?'*12+'.'+domain
    names=[role+'.'+suffix for role in ('gateway','keycloak','observability','_enmaas-owner')]
    prefix='route53:ChangeResourceRecordSets'
    resource='arn:aws:route53:::hostedzone/'+zone
    return {'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['route53:GetHostedZone','route53:ListResourceRecordSets'],'Resource':resource},
        {'Effect':'Allow','Action':'route53:ChangeResourceRecordSets','Resource':resource,
         'Condition':{'ForAllValues:StringLike':{prefix+'NormalizedRecordNames':names},
                      'ForAllValues:StringEquals':{prefix+'RecordTypes':['A','TXT'],prefix+'Actions':['CREATE','DELETE']},
                      'Null':{prefix+name:'false' for name in ('NormalizedRecordNames','RecordTypes','Actions')}}}]}


def plan(manifest,principal,images,types,max_volume_gib,allow_role=False,*,dns_zone=None,dns_domain=None):
    registry=vm.network.registry
    trust=registry.trust_policy(manifest,principal,allow_role)
    documents=[vm.network.network_policy(manifest),vm.vm_policy(manifest,images,types,max_volume_gib)]
    dns_scope=None
    if dns_zone is not None or dns_domain is not None:
        documents.append(dns_policy(manifest,dns_zone,dns_domain))
        dns_scope={'zone_id':dns_zone,'domain':dns_domain}
    policies=packing.pack(documents)
    role_name='em-'+manifest['group']+'-operator'
    return {'account':manifest['account'],'region':manifest['region'],'group':manifest['group'],
            'role_name':role_name,'role_arn':'arn:aws:iam::'+manifest['account']+':role/enmaas/'+role_name,
            'trust':trust,'policies':policies,'max_session_seconds':3600,
            'rds_supported':False,'cleanup_supported':False,'dns_scope':dns_scope,'aws_changes_made':False,
            'qualification':'Network/VM grants and explicitly selected DNS namespace only; live IAM allow/deny tests remain required'}


def policy_arn(proposal,index):
    return 'arn:aws:iam::'+proposal['account']+':policy/enmaas/'+proposal['role_name']+'-'+str(index+1).zfill(2)


def preflight(aws,proposal):
    identity=aws.call('sts','get-caller-identity')
    if (identity.get('Account')!=proposal['account'] or not re.fullmatch(
            r'arn:aws:(?:iam|sts)::'+proposal['account']+r':(?:user|assumed-role|federated-user)/[^\s]+',identity.get('Arn',''))):
        raise ValueError('Bootstrap requires a non-root identity in the selected account')
    for action,options in [('get-role',{'RoleName':proposal['role_name']})]+[
            ('get-policy',{'PolicyArn':policy_arn(proposal,i)}) for i in range(len(proposal['policies']))]:
        try:aws.call('iam',action,**options)
        except AwsFailure as error:
            if error.code!='NoSuchEntity':raise
        else:raise ValueError('IAM role/policy name already exists; no existing resources are adopted')
    quotas=aws.call('iam','get-account-summary')['SummaryMap']
    if (quotas['Roles']+1>quotas['RolesQuota']
            or quotas['Policies']+len(proposal['policies'])>quotas['PoliciesQuota']
            or len(proposal['policies'])>quotas['AttachedPoliciesPerRoleQuota']):
        raise ValueError('IAM account or attachment quota is insufficient')
    for document,kind in [(proposal['trust'],'RESOURCE_POLICY')]+[(p,'IDENTITY_POLICY') for p in proposal['policies']]:
        options={'policyDocument':json.dumps(document,separators=(',',':')),'policyType':kind}
        if kind=='RESOURCE_POLICY':options['validatePolicyResourceType']='AWS::IAM::AssumeRolePolicyDocument'
        result=aws.call('accessanalyzer','validate-policy',**options)
        if result.get('nextToken') or result.get('NextToken') or result['findings']:
            raise ValueError('Access Analyzer findings or incomplete results require review before bootstrap')


def apply(aws,proposal,path,confirmation):
    if confirmation!='create operator '+proposal['group']+' '+proposal['account']:
        raise ValueError('Exact operator-group/account confirmation is required')
    preflight(aws,proposal)
    journal=network_create.Journal(path)
    journal.append({'event':'plan','account':proposal['account'],'region':proposal['region'],
                    'group':proposal['group'],'role_arn':proposal['role_arn'],'trust':proposal['trust'],
                    'policy_sha256':[hashlib.sha256(json.dumps(p,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                                     for p in proposal['policies']]})
    tags=[{'Key':'enmaas:group','Value':proposal['group']},{'Key':'enmaas:purpose','Value':'external-services-operator'}]

    def write(action,options,container=None,expected_arn=None):
        journal.append({'event':'intent','action':action,'options':options})
        try:
            result=aws.call('iam',action,**options)
            if container:
                resource=result[container]
                if resource['Arn']!=expected_arn:raise ValueError('Unexpected IAM resource ARN')
                identifier=resource[container+'Id']
                if not isinstance(identifier,str) or not re.fullmatch(r'(?:AROA|ANPA)[A-Z0-9]{12,124}',identifier):
                    raise ValueError('Unexpected IAM resource ID')
            else:identifier=None
        except (OSError,ValueError,TypeError,KeyError):
            raise ValueError('IAM operation outcome is uncertain; preserve the journal and inspect resources before retrying') from None
        journal.append({'event':'result','action':action,'arn':expected_arn,'id':identifier})

    write('create-role',{'RoleName':proposal['role_name'],'Path':'/enmaas/',
          'AssumeRolePolicyDocument':json.dumps(proposal['trust'],separators=(',',':')),
          'MaxSessionDuration':3600,'Tags':tags},'Role',proposal['role_arn'])
    for index,document in enumerate(proposal['policies']):
        arn=policy_arn(proposal,index)
        write('create-policy',{'PolicyName':arn.rsplit('/',1)[1],'Path':'/enmaas/',
              'PolicyDocument':json.dumps(document,separators=(',',':')),'Tags':tags},'Policy',arn)
        write('attach-role-policy',{'RoleName':proposal['role_name'],'PolicyArn':arn})
    result={'role_arn':proposal['role_arn'],'policy_arns':[policy_arn(proposal,i) for i in range(len(proposal['policies']))],
            'journal':str(path),'live_permissions_qualified':False,'access_keys_created':False}
    journal.append({'event':'complete',**result})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--manifest',type=Path)
    selection.add_argument('--group')
    parser.add_argument('--account')
    parser.add_argument('--region',default='eu-central-1',help='region for group bootstrap; a manifest supplies its own region')
    parser.add_argument('--principal-arn',required=True)
    parser.add_argument('--allow-role-principal',action='store_true')
    parser.add_argument('--image-id',action='append',required=True)
    parser.add_argument('--instance-type',action='append',required=True)
    parser.add_argument('--max-volume-gib',type=int,required=True)
    parser.add_argument('--dns-zone-id',help='optional approved Route 53 hosted zone ID; requires --dns-domain')
    parser.add_argument('--dns-domain',help='optional approved domain/suffix; grants only group deployment names')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--admin-profile')
    parser.add_argument('--journal',type=Path)
    parser.add_argument('--confirm')
    args=parser.parse_args()
    if args.manifest:
        manifest=vm.network.registry.load(args.manifest)
    else:
        if not args.account:raise ValueError('--group requires --account')
        # Scope adapter only: no deployment registry or SSH key is created.
        manifest=vm.network.registry.new(args.account,args.region,'/unused-ssh-key','operator-preview',group=args.group)
    proposal=plan(manifest,args.principal_arn,args.image_id,
                  args.instance_type,args.max_volume_gib,args.allow_role_principal,dns_zone=args.dns_zone_id,dns_domain=args.dns_domain)
    if args.apply:
        if not args.admin_profile or not args.journal or not args.confirm:
            raise ValueError('--apply requires --admin-profile, --journal and --confirm')
        result=apply(Aws(args.admin_profile,proposal['region']),proposal,args.journal,args.confirm)
    else:result=proposal
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,TypeError) as error:
        raise SystemExit('Operator bootstrap stopped: '+str(error)) from None
