#!/usr/bin/env python3
"""Inspect local network creation records without AWS calls or deletion authority."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat

spec=importlib.util.spec_from_file_location('network_creation',Path(__file__).with_name('vpc-create.py'))
creation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(creation)
registry=creation.planner.registry
PREFIXES={'create-vpc':'vpc','create-internet-gateway':'igw','create-route-table':'rtb',
          'create-subnet':'subnet','associate-route-table':'rtbassoc'}


def inspect(path,manifest):
    registry.validate(manifest)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('Journal directory must be private and user-owned')
    paths=sorted(path.iterdir())
    if not paths or len(paths)>256 or [p.name for p in paths]!=[str(i).zfill(4)+'.json' for i in range(len(paths))]:
        raise ValueError('Journal is empty, oversized or has unexpected/missing records')
    events=[registry.read_private_json(p) for p in paths]
    first=events[0]
    if (set(first)!={'event','manifest','plan'} or first['event']!='plan'
            or first['manifest']!=manifest or first['plan'].get('deployment')!=manifest['id']):
        raise ValueError('Journal plan belongs to a different deployment or format')
    pending=None;complete=False;confirmed=[];seen=set()
    for event in events[1:]:
        if complete:raise ValueError('Journal contains records after completion')
        kind=event.get('event')
        if kind=='intent':
            if (pending is not None or set(event)!={'event','action','options'}
                    or event['action'] not in creation.Aws.ACTIONS or not isinstance(event['options'],dict)):
                raise ValueError('Invalid or overlapping operation intent')
            pending=event
        elif kind=='result':
            if (pending is None or set(event)!={'event','action','id'} or event['action']!=pending['action']):
                raise ValueError('Result has no matching intent')
            prefix=PREFIXES.get(event['action']);identifier=event['id']
            if prefix:
                if not isinstance(identifier,str) or not re.fullmatch(prefix+r'-[a-f0-9]{8,17}',identifier) or identifier in seen:
                    raise ValueError('Invalid or duplicate resource identifier')
                seen.add(identifier)
                confirmed.append({'action':event['action'],'id':identifier})
            elif identifier is not None:raise ValueError('Unexpected resource identifier for mutation')
            pending=None
        elif kind=='complete':
            if (pending is not None or set(event)!={'event','resources','runtime_verified'}
                    or event['runtime_verified'] is not False or not isinstance(event['resources'],dict)):
                raise ValueError('Invalid completion record')
            complete=True
        else:raise ValueError('Unknown network journal record')
    state='creation-recorded-complete' if complete else ('uncertain-operation' if pending else 'interrupted-between-operations')
    return {'deployment':manifest['id'],'account':manifest['account'],'region':manifest['region'],
            'state':state,'confirmed_resources':confirmed,
            'pending_operation':{'action':pending['action']} if pending else None,
            'live_verified':False,'deletion_authorized':False,
            'next_action':'Compare live account, resource ownership tags and relationships before recovery or cleanup; never retry an uncertain write blindly'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--journal',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(inspect(args.journal,registry.load(args.manifest)),indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,TypeError,KeyError,AttributeError):
        raise SystemExit('Network journal inspection stopped: invalid, inaccessible or inconsistent records; preserve them for manual review') from None
