#!/usr/bin/env python3
"""Choose a public hosted zone from AWS discovery JSON; no AWS calls or files written."""
import json
import re
import shlex
import sys


def catalog(document):
    if (not isinstance(document,dict) or not isinstance(document.get('HostedZones'),list)
            or document.get('NextToken') or document.get('IsTruncated')):
        raise ValueError('Incomplete hosted-zone response')
    rows=[];seen=set()
    for row in document['HostedZones']:
        private=row.get('Config',{}).get('PrivateZone')
        if type(private) is not bool:raise ValueError('Missing public/private zone classification')
        if private:continue
        identifier=row['Id'].removeprefix('/hostedzone/');domain=row['Name'].removesuffix('.')
        if (not re.fullmatch(r'Z[A-Z0-9]+',identifier) or identifier in seen
                or len(domain)>190 or len(domain.split('.'))<2
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label)
                       for label in domain.split('.'))):
            raise ValueError('Invalid or duplicate public-zone metadata')
        seen.add(identifier);rows.append((identifier,domain))
    return sorted(rows,key=lambda row:(row[1],row[0]))


def select(rows,read,terminal):
    if not rows:
        terminal.write('No public hosted zones available; DNS remains unselected.\n')
        return '',''
    terminal.write('\nChoose a zone you are authorized to use (0 skips DNS):\n')
    for index,(identifier,domain) in enumerate(rows,1):
        terminal.write(f'  {index}. {domain} ({identifier})\n')
    terminal.write('Public zone [0]: ');terminal.flush()
    answer=read().strip() or '0'
    if not re.fullmatch(r'[0-9]{1,5}',answer) or int(answer)>len(rows):
        raise ValueError('Choose a listed zone number or 0')
    return rows[int(answer)-1] if int(answer) else ('','')


def main():
    rows=catalog(json.load(sys.stdin))
    with open('/dev/tty','r') as reader, open('/dev/tty','w') as terminal:
        zone,domain=select(rows,reader.readline,terminal)
    print('export ZONE_ID='+shlex.quote(zone))
    print('export DNS_DOMAIN='+shlex.quote(domain))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,TypeError,AttributeError):
        raise SystemExit('Zone selection stopped; no deployment settings or AWS resources changed.') from None
