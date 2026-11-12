#!/usr/bin/env python3
"""Read-only, conservative firewalld audit for dedicated lab hosts.

This checks firewalld configuration, not arbitrary nftables rules or AWS packet
reachability. Unrecognized policies require review rather than an automatic bypass.
"""
import argparse
import os
import re
import shlex
import subprocess


def parse_info(text):
    fields = {}
    rules = []
    in_rules = False
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        if in_rules:
            rules.append(tuple(shlex.split(line.strip())))
            continue
        key, separator, value = line.strip().partition(':')
        if not separator or key in fields:
            raise ValueError('unrecognized or repeated firewalld property')
        fields[key] = value.strip()
        if key == 'rich rules':
            if value.strip():
                raise ValueError('unexpected inline rich rule')
            in_rules = True
    return fields, rules


def validate_zone(text, allowed_rules):
    fields, rules = parse_info(text)
    expected = {'target', 'interfaces', 'sources', 'services', 'ports', 'protocols',
                'forward', 'masquerade', 'forward-ports', 'source-ports',
                'icmp-blocks', 'rich rules', 'icmp-block-inversion', 'priority',
                'ingress-priority', 'egress-priority'}
    if set(fields)-expected or not {'target', 'services', 'ports', 'protocols',
                                    'masquerade', 'forward-ports', 'source-ports', 'rich rules'} <= set(fields):
        raise ValueError('unknown or missing firewalld properties')
    if fields['target'] not in ('default', 'DROP', 'REJECT'):
        raise ValueError('zone accepts traffic without an explicit rule')
    if set(fields['services'].split()) - {'ssh', 'dhcpv6-client'}:
        raise ValueError('zone exposes additional services; review before installation')
    for field in ('ports', 'protocols', 'forward-ports', 'source-ports'):
        if fields[field]:
            raise ValueError('zone has unreviewed ' + field)
    if fields['masquerade'] != 'no':
        raise ValueError('zone masquerading requires separate review')
    allowed = {tuple(shlex.split(rule)) for rule in allowed_rules}
    if set(rules)-allowed:
        raise ValueError('zone has rich rules outside the requested source/port allowlist')



def validate_policy(text):
    fields, rules = parse_info(text)
    required = {'target': 'CONTINUE', 'ingress-zones': 'ANY', 'egress-zones': 'HOST',
                'priority': '-15000', 'services': '', 'ports': '', 'protocols': '',
                'masquerade': 'no', 'forward-ports': '', 'source-ports': '',
                'icmp-blocks': '', 'rich rules': ''}
    if fields != required:
        raise ValueError('IPv6 host policy differs from reviewed settings')
    types = ('neighbour-advertisement', 'neighbour-solicitation', 'router-advertisement',
             'redirect', 'mld-listener-done', 'mld-listener-query',
             'mld-listener-report', 'mld2-listener-report')
    allowed = {('rule', 'family=ipv6', 'icmp-type', 'name='+name, 'accept') for name in types}
    if set(rules) - allowed:
        raise ValueError('IPv6 host policy permits traffic beyond reviewed ICMPv6 types')


def validate_service(name, text):
    fields, rules = parse_info(text)
    expected_port = {'ssh': '22/tcp', 'dhcpv6-client': '546/udp'}[name]
    allowed = {'ports', 'protocols', 'source-ports', 'modules', 'destination',
               'includes', 'helpers', 'short', 'description'}
    if rules or set(fields)-allowed or fields.get('ports') != expected_port:
        raise ValueError('service definition differs from reviewed ports: '+name)
    for key in ('protocols', 'source-ports', 'modules', 'includes', 'helpers'):
        if fields.get(key):
            raise ValueError('service definition enables additional behavior: '+name)
    destination = fields.get('destination', '')
    if destination not in (('', 'ipv6: fe80::/64') if name == 'dhcpv6-client' else ('',)):
        raise ValueError('service destination requires review: '+name)

def query(*args):
    result = subprocess.run(['firewall-cmd', *args], capture_output=True, text=True,
                            timeout=15, env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        raise ValueError('firewalld query failed; no rules changed: ' + ' '.join(args))
    return result.stdout.strip()


def audit(allowed_rules, run=query):
    if run('--state') != 'running':
        raise ValueError('firewalld must be running')
    default = run('--get-default-zone')
    active = {line.strip() for line in run('--get-active-zones').splitlines()
              if line and not line[0].isspace()}
    zones = active | {default}
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', zone) for zone in zones):
        raise ValueError('invalid zone name')
    for permanent in (False, True):
        mode = ['--permanent'] if permanent else []
        # Permanent interface/source bindings may become active after reboot.
        if permanent:
            for zone in run(*mode, '--get-zones').split():
                if (run(*mode, '--zone='+zone, '--list-interfaces') or
                        run(*mode, '--zone='+zone, '--list-sources')):
                    zones.add(zone)
        if run(*mode, '--direct', '--get-all-rules') or run(*mode, '--direct', '--get-all-passthroughs'):
            raise ValueError('direct rules/passthroughs require separate review')
        for policy in run(*mode, '--get-policies').split():
            if policy != 'allow-host-ipv6':
                raise ValueError('firewalld policies require separate review; no policies were changed')
            validate_policy(run(*mode, '--policy='+policy, '--list-all'))
        for service in ('ssh', 'dhcpv6-client'):
            validate_service(service, run(*mode, '--info-service='+service))
        for zone in sorted(zones):
            validate_zone(run(*mode, '--zone='+zone, '--list-all'), allowed_rules)
    return sorted(zones)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-rule', action='append', default=[])
    args = parser.parse_args()
    zones = audit(args.allow_rule)
    print('PASS: reviewed runtime/permanent firewalld configuration for ' + ', '.join(zones))
    print('Still required: independent nftables rules, AWS rules and packet-level deny probes.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error))
