#!/usr/bin/env python3
"""Read-only VM security-group cleanup plan after journaled termination.

Reports interface/foreign-reference blockers. Never edits rules or deletes groups.
This is not a complete VPC dependency audit or deletion authorization.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re

spec=importlib.util.spec_from_file_location('group_termination',Path(__file__).with_name('cleanup-instances.py'))
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)


class Aws(t.Aws):
    WRITES=set()


def recorded_groups(manifest,journals):
    t.cleanup_policy(manifest,journals)
    if set(journals)!=set(t.inventory.ROLES):raise ValueError('Expected exactly three VM journals')
    groups={};vpcs=set()
    for state in journals.values():
        group=state.get('SecurityGroupId','');vpc=state.get('VpcId','')
        if (not re.fullmatch(r'sg-[a-f0-9]{8,17}',group) or group in groups
                or not re.fullmatch(r'vpc-[a-f0-9]{8,17}',vpc)):
            raise ValueError('Expected unique exact security groups and VPC identities')
        groups[group]=state;vpcs.add(vpc)
    if len(vpcs)!=1:raise ValueError('VM groups must belong to one recorded VPC')
    return groups


def plan(aws,manifest,journals,termination_journal):
    expected=recorded_groups(manifest,journals)
    if t.verify_termination(aws,manifest,journals,termination_journal)['termination_verified'] is not True:
        raise ValueError('Verify journaled VM termination and root-volume removal first')
    response=aws.call('ec2','describe-security-groups',GroupIds=sorted(expected))
    rows=response['SecurityGroups']
    if response.get('NextToken') or len(rows)!=len(expected) or {row['GroupId'] for row in rows}!=set(expected):
        raise ValueError('Incomplete, missing, duplicate or foreign security groups')
    for row in rows:
        state=expected[row['GroupId']]
        if (row.get('OwnerId')!=manifest['account'] or row.get('VpcId')!=state['VpcId']
                or row.get('GroupName')=='default'
                or not t.inventory.vm.owned_tags(row,state['Prefix'],state['Scenario'])):
            raise ValueError('Security group ownership or VPC differs')
    # The shared inventory functions use snake_case CLI options. This client
    # passes JSON to AWS, so normalize the same way as the termination inspector.
    class Reader:
        def call(self,service,action,**options):
            return aws.call(service,action,**{''.join(part.title() for part in key.split('_')):value
                                            for key,value in options.items()})
    reader=Reader()
    interfaces=t.inventory.network_dependencies(reader,manifest['account'],set(expected),set())
    references=t.inventory.group_references(reader,manifest['account'],set(expected))
    foreign=[row for row in references if not row['source_owned']]
    blockers=[]
    if interfaces:blockers.append({'kind':'remaining-network-interfaces','resources':interfaces})
    if foreign:blockers.append({'kind':'foreign-security-group-references','resources':foreign})
    steps=[] if blockers else [
        {'stage':'remove-internal-references','references':references},
        {'stage':'remove-owned-groups','group_ids':sorted(expected)}]
    return {'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
            'vpc':next(iter(expected.values()))['VpcId'],'blockers':blockers,'steps':steps,
            'writes_performed':False,'deletion_authorized':False,
            'unverified':['cross-account/peered references','VPC-wide service dependencies','concurrent changes'],
            'next_action':'Review blockers; revalidate live ownership before any mutation. No deletion commands are generated.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--termination-journal',required=True,type=Path)
    args=parser.parse_args();manifest=t.inventory.registry.load(args.manifest)
    journals={role:t.inventory.registry.read_private_json(args.state_dir/(manifest['id']+'-'+role+'.json'))
              for role in t.inventory.ROLES}
    print(json.dumps(plan(Aws(manifest),manifest,journals,args.termination_journal),indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        raise SystemExit('Group cleanup planning stopped; preserve journals and inspect live dependencies. No writes performed.') from None
