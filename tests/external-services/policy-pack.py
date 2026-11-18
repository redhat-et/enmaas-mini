#!/usr/bin/env python3
"""Pack reviewed identity-policy fragments into bounded documents without changing grants."""
import argparse
import copy
import json
from pathlib import Path


def pack(documents,max_chars=6144,max_policies=10):
    if not documents or not 1<=max_chars<=6144 or not 1<=max_policies<=10:
        raise ValueError('Supply policy fragments and a supported document/attachment budget')
    result=[];current={'Version':'2012-10-17','Statement':[]};sequence=0
    for document in documents:
        if (not isinstance(document,dict) or set(document)!={'Version','Statement'}
                or document['Version']!='2012-10-17' or not isinstance(document['Statement'],list)
                or not document['Statement']):
            raise ValueError('Expected a nonempty identity-policy fragment')
        for statement in document['Statement']:
            if not isinstance(statement,dict):raise ValueError('Expected a policy statement object')
            statement=copy.deepcopy(statement);statement['Sid']='Grant'+str(sequence);sequence+=1
            candidate={'Version':'2012-10-17','Statement':current['Statement']+[statement]}
            if len(json.dumps(candidate,separators=(',',':')))>max_chars:
                if current['Statement']:result.append(current)
                candidate={'Version':'2012-10-17','Statement':[statement]}
                if len(json.dumps(candidate,separators=(',',':')))>max_chars:
                    raise ValueError('One statement exceeds the managed-policy size budget')
            current=candidate
    result.append(current)
    if len(result)>max_policies:raise ValueError('Policy bundle exceeds the attachment budget')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fragments',type=Path,nargs='+')
    args=parser.parse_args()
    documents=pack([json.loads(path.read_text()) for path in args.fragments])
    print(json.dumps({'policies':documents,'iam_validated':False,'aws_changes_made':False},indent=2))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as error:raise SystemExit('Policy packing stopped: '+str(error)) from None
