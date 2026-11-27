#!/usr/bin/env python3
"""Assign fixed private addresses to the MaaS, gateway and metering containers."""
import argparse
import ipaddress
from itertools import chain
import json
from pathlib import Path
import sys


def service_addresses(networks):
    if not isinstance(networks, list) or len(networks) != 1:
        raise ValueError('expected exactly one private Podman network')
    network = networks[0]
    subnets = [item for item in network.get('subnets', [])
               if ':' not in item.get('subnet', '')]
    if len(subnets) != 1:
        raise ValueError('expected exactly one IPv4 private subnet')
    subnet_data = subnets[0]
    subnet = ipaddress.ip_network(subnet_data['subnet'])
    if not subnet.is_private:
        raise ValueError('MaaS network must use a private IPv4 subnet')
    gateway = ipaddress.ip_address(subnet_data['gateway'])
    # podman 5's network inspect lists attached containers with their
    # interfaces; podman 4.9 omits the map entirely. The degradation is
    # harmless: the preferred addresses selected below (and the migration's
    # .13) sit above the bottom of the range where the dynamically attached
    # database leases sequentially, so the selection stays collision-free
    # on both versions — the map only preserves already-running units'
    # addresses and guards against foreign containers on podman 5.
    containers = network.get('containers') or {}
    used = set()
    current = {}
    for item in containers.values():
        addresses = [subnet['ipnet'].split('/', 1)[0]
                     for interface in item.get('interfaces', {}).values()
                     for subnet in interface.get('subnets', [])
                     if ':' not in subnet.get('ipnet', '')]
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if item.get('name') in ('pricetag-maas', 'pricetag-gateway', 'pricetag-metering'):
                current[item['name']] = parsed
            else:
                used.add(parsed)
    selected = {}
    names = ('pricetag-maas', 'pricetag-gateway', 'pricetag-metering')
    for name in names:
        address = current.get(name)
        if address is not None and address in subnet and address not in used and address != gateway:
            selected[name] = address
            used.add(address)
    for name, offset in (('pricetag-maas', 10), ('pricetag-gateway', 11), ('pricetag-metering', 12)):
        if name in selected:
            continue
        preferred = ipaddress.ip_address(int(subnet.network_address) + offset)
        hosts = subnet.hosts()
        candidates = chain([preferred] if preferred in subnet else [],
                           (candidate for candidate in hosts if candidate > preferred),
                           (candidate for candidate in subnet.hosts() if candidate < preferred))
        address = next((candidate for candidate in candidates
                        if candidate not in used and candidate != gateway), None)
        if address is None:
            raise ValueError('no free private addresses for MaaS, Praxis and PriceTag')
        selected[name] = address
        used.add(address)
    # The one-shot schema-migration container joins the same network before
    # the service units start. Give it a fixed address of its own, disjoint
    # from every service address above, so its dynamic allocation can never
    # take a preferred service address and a failed cleanup that leaves the
    # container behind cannot collide with the static addresses the units
    # claim afterwards.
    preferred = ipaddress.ip_address(int(subnet.network_address) + 13)
    hosts = subnet.hosts()
    candidates = chain([preferred] if preferred in subnet else [],
                       (candidate for candidate in hosts if candidate > preferred),
                       (candidate for candidate in subnet.hosts() if candidate < preferred))
    migration = next((candidate for candidate in candidates
                      if candidate not in used and candidate != gateway), None)
    if migration is None:
        raise ValueError('no free private address for the MaaS schema migration')
    selected['pricetag-migrate'] = migration
    return {name.removeprefix('pricetag-'): str(address) for name, address in selected.items()}


def configure_units(maas_path, gateway_path, metering_path, addresses):
    parsed = {name: ipaddress.ip_address(addresses[name])
              for name in ('maas', 'gateway', 'metering')}
    if any(address.version != 4 or not address.is_private for address in parsed.values()):
        raise ValueError('selected service addresses must be private IPv4 addresses')
    maas_file, gateway_file, metering_file = map(Path, (maas_path, gateway_path, metering_path))
    maas_lines = maas_file.read_text().splitlines()
    gateway_lines, metering_lines = gateway_file.read_text().splitlines(), metering_file.read_text().splitlines()
    indexes = [i for i, line in enumerate(maas_lines) if line.startswith('IP=')]
    if len(indexes) > 1:
        raise ValueError('expected at most one static MaaS address')
    entry = 'IP=' + str(parsed['maas'])
    if indexes:
        maas_lines[indexes[0]] = entry
    else:
        container = maas_lines.index('[Container]')
        maas_lines.insert(container + 1, entry)
    for lines, name, label in ((gateway_lines, 'gateway', 'gateway'),
                               (metering_lines, 'metering', 'metering')):
        networks = [i for i, line in enumerate(lines)
                    if line.startswith('Network=pricetag.network')]
        if len(networks) != 1:
            raise ValueError('expected exactly one ' + label + ' connection to pricetag.network')
        lines[networks[0]] = 'Network=pricetag.network:ip=' + str(parsed[name])
    maas_file.write_text('\n'.join(maas_lines) + '\n')
    gateway_file.write_text('\n'.join(gateway_lines) + '\n')
    metering_file.write_text('\n'.join(metering_lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('addresses')
    configure = subparsers.add_parser('configure')
    configure.add_argument('maas_unit', type=Path)
    configure.add_argument('gateway_unit', type=Path)
    configure.add_argument('metering_unit', type=Path)
    args = parser.parse_args()
    if args.command == 'addresses':
        print(json.dumps(service_addresses(json.load(sys.stdin)), sort_keys=True))
    else:
        configure_units(args.maas_unit, args.gateway_unit, args.metering_unit, json.load(sys.stdin))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
