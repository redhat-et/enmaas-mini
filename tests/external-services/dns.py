#!/usr/bin/env python3
"""Manage exact deployment DNS records; no zone/domain or certificate changes.

Requires an existing public hosted zone and AWS profile. Creation/deletion require
an explicit confirmation string. Journals are immutable ownership evidence; keep
them through cleanup. No credentials are accepted as arguments or printed.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess

spec=importlib.util.spec_from_file_location('dns_registry',Path(__file__).with_name('deployments.py'))
registry=importlib.util.module_from_spec(spec);spec.loader.exec_module(registry)
ROLES=('gateway','keycloak','observability')


def records(manifest,addresses,nonce):
    registry.validate(manifest)
    if not manifest['zone_id'] or set(addresses)!=set(ROLES) or not re.fullmatch(r'[a-f0-9]{32}',nonce):
        raise ValueError('DNS requires a zone, all three addresses and a valid ownership nonce')
    suffix=manifest['id']+'.'+manifest['domain']+'.'
    result=[]
    for role in ROLES:
        address=ipaddress.IPv4Address(addresses[role])
        if not address.is_global:raise ValueError('public DNS requires explicit global IPv4 addresses')
        result.append({'Name':role+'.'+suffix,'Type':'A','TTL':60,
                       'ResourceRecords':[{'Value':str(address)}]})
    result.append({'Name':'_enmaas-owner.'+suffix,'Type':'TXT','TTL':60,
                   'ResourceRecords':[{'Value':'"'+manifest['account']+':'+manifest['id']+':'+nonce+'"'}]})
    return result


def validate_zone(manifest,zone):
    domain=manifest['domain'];name=zone['Name'].lower().rstrip('.')
    if (zone['Id'].removeprefix('/hostedzone/')!=manifest['zone_id'] or
            zone.get('Config',{}).get('PrivateZone') is not False or
            not (domain==name or domain.endswith('.'+name))):
        raise ValueError('hosted zone ID, public scope or domain relationship differs')


def delete_batch(expected,live):
    # Route 53 DELETE also requires exact values/TTL, protecting the check/write race.
    canonical=lambda rows:sorted(json.dumps(row,sort_keys=True) for row in rows)
    if canonical(expected)!=canonical(live):
        raise ValueError('DNS records differ from the immutable ownership journal')
    return {'Changes':[{'Action':'DELETE','ResourceRecordSet':row} for row in expected]}


class Aws:
    def __init__(self,manifest):self.manifest=manifest
    def call(self,service,action,**options):
        if (service,action) not in {('sts','get-caller-identity'),('route53','get-hosted-zone'),
                ('route53','list-resource-record-sets'),('route53','change-resource-record-sets')}:
            raise ValueError('AWS action not allowed')
        command=['aws','--no-cli-pager','--no-paginate','--profile',self.manifest['aws_profile'],
                 '--region',self.manifest['region'],'--output','json',service,action,
                 '--cli-input-json',json.dumps(options)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=60)
        if result.returncode:raise ValueError('AWS DNS operation failed; inspect the journal before retrying')
        return json.loads(result.stdout)


def live_records(aws,manifest,expected):
    result=[]
    for record in expected:
        response=aws.call('route53','list-resource-record-sets',HostedZoneId=manifest['zone_id'],
                          StartRecordName=record['Name'],StartRecordType=record['Type'],MaxItems='1')
        values=response['ResourceRecordSets']
        if values and values[0]['Name'].lower()==record['Name']:result.append(values[0])
    return result


def write_journal(path,value):
    info=path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('journal parent must be a private user-owned directory')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())


def load_journal(path,manifest):
    journal=registry.read_private_json(path)
    if (set(journal)!={'version','manifest','addresses','nonce','records'} or journal['version']!=1
            or journal['manifest']!=manifest):
        raise ValueError('journal belongs to a different deployment/configuration')
    if journal['records']!=records(manifest,journal['addresses'],journal['nonce']):
        raise ValueError('journal records exceed the selected deployment scope')
    return journal


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','create','status','delete'))
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--journal',type=Path,required=True)
    for role in ROLES:parser.add_argument('--'+role+'-ip')
    parser.add_argument('--confirm',help='ACTION DEPLOYMENT_ID ACCOUNT_ID; required for cloud writes')
    args=parser.parse_args();manifest=registry.load(args.manifest)
    if not manifest['zone_id']:parser.error('selected deployment has no DNS configuration')
    addresses={role:getattr(args,role+'_ip') for role in ROLES}
    if args.journal.exists() or args.journal.is_symlink():
        journal=load_journal(args.journal,manifest)
        if any(addresses.values()) and addresses!=journal['addresses']:
            parser.error('addresses differ from the immutable journal')
    else:
        if args.action not in ('plan','create'):parser.error('existing ownership journal required')
        journal={'version':1,'manifest':manifest,'addresses':addresses,'nonce':secrets.token_hex(16)}
        journal['records']=records(manifest,addresses,journal['nonce'])
    aws=Aws(manifest)
    identity=aws.call('sts','get-caller-identity')
    if identity['Account']!=manifest['account'] or identity['Arn'].endswith(':root'):
        raise ValueError('wrong AWS account or root credentials')
    validate_zone(manifest,aws.call('route53','get-hosted-zone',Id=manifest['zone_id'])['HostedZone'])
    expected=journal['records'];live=live_records(aws,manifest,expected)
    exact=sorted(json.dumps(r,sort_keys=True) for r in live)==sorted(json.dumps(r,sort_keys=True) for r in expected)
    state='matching' if exact else 'absent' if not live else 'conflict'
    if args.action in ('plan','status'):
        print(json.dumps({'deployment':manifest['id'],'state':state,'records':expected,
                          'delegation_verified':False,'changes_made':False},indent=2));return
    if args.confirm!=args.action+' '+manifest['id']+' '+manifest['account']:
        parser.error('supply the exact action, deployment ID and account confirmation')
    if args.action=='create':
        if exact:
            print('Records already match the journal; no changes.');return
        if live:raise ValueError('DNS collision; existing records will not be overwritten')
        if not args.journal.exists():write_journal(args.journal,journal)
        batch={'Changes':[{'Action':'CREATE','ResourceRecordSet':row} for row in expected]}
    else:
        if not live:
            print('Journaled records absent; no DNS changes. Keep the journal for audit.');return
        batch=delete_batch(expected,live)
    changed=aws.call('route53','change-resource-record-sets',HostedZoneId=manifest['zone_id'],ChangeBatch=batch)
    print(json.dumps({'change':changed['ChangeInfo'],'journal':str(args.journal),
                      'note':'Wait for DNS propagation; zone and journal retained.'},indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError,subprocess.SubprocessError) as error:
        raise SystemExit('DNS operation stopped: '+str(error)) from None
