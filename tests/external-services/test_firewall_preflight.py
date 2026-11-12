import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('firewall_preflight', Path(__file__).with_name('firewall-preflight.py'))
firewall = importlib.util.module_from_spec(spec)
spec.loader.exec_module(firewall)

BASE = '''public (active)
  target: default
  interfaces: eth0
  sources:
  services: ssh dhcpv6-client
  ports:
  protocols:
  forward: no
  masquerade: no
  forward-ports:
  source-ports:
  icmp-blocks:
  rich rules:
'''

class Firewall(unittest.TestCase):
    def test_baseline_and_exact_requested_rule(self):
        allowed = ['rule family=ipv4 source address=192.0.2.1/32 port port=8443 protocol=tcp accept']
        firewall.validate_zone(BASE, allowed)
        firewall.validate_zone(BASE+'    rule family="ipv4" source address="192.0.2.1/32" port port="8443" protocol="tcp" accept\n', allowed)

    def test_existing_broader_access_is_rejected(self):
        for old, new in [('target: default', 'target: ACCEPT'), ('ports:', 'ports: 24443/tcp'),
                         ('protocols:', 'protocols: tcp'), ('services: ssh dhcpv6-client', 'services: ssh cockpit'),
                         ('masquerade: no', 'masquerade: yes'), ('forward-ports:', 'forward-ports: port=80:proto=tcp:toport=24443'),
                         ('source-ports:', 'source-ports: 443/tcp')]:
            with self.subTest(new=new), self.assertRaises(ValueError):
                firewall.validate_zone(BASE.replace(old,new), [])
        with self.assertRaises(ValueError):
            firewall.validate_zone(BASE+'    rule family=ipv4 source address=0.0.0.0/0 port port=8443 protocol=tcp accept\n', [])

    def test_missing_or_unknown_properties_refuse_qualification(self):
        for text in (BASE.replace('  target: default\n',''), BASE+'  new-feature: enabled\n'):
            with self.assertRaises(ValueError):firewall.validate_zone(text, [])

    def test_audit_checks_permanent_rules_and_rejects_policies(self):
        calls = []
        def query(*args):
            calls.append(args)
            if '--state' in args:return 'running'
            if '--get-default-zone' in args:return 'public'
            if '--get-active-zones' in args:return 'public\n  interfaces: eth0'
            if '--get-zones' in args:return 'public'
            if '--list-all' in args:return BASE
            if '--info-service=ssh' in args:return 'ssh\n  ports: 22/tcp'
            if '--info-service=dhcpv6-client' in args:return 'dhcpv6-client\n  ports: 546/udp'
            return ''
        self.assertEqual(firewall.audit([], query), ['public'])
        self.assertIn(('--permanent', '--zone=public', '--list-all'), calls)
        def policy(*args):
            if '--get-policies' in args:return 'unreviewed-policy'
            return query(*args)
        with self.assertRaisesRegex(ValueError,'policies'):
            firewall.audit([], policy)

    def test_standard_ipv6_policy_does_not_permit_tcp(self):
        policy = """allow-host-ipv6 (active)
  priority: -15000
  target: CONTINUE
  ingress-zones: ANY
  egress-zones: HOST
  services:
  ports:
  protocols:
  masquerade: no
  forward-ports:
  source-ports:
  icmp-blocks:
  rich rules:
    rule family="ipv6" icmp-type name="neighbour-advertisement" accept
"""
        firewall.validate_policy(policy)
        for bad in (policy.replace('CONTINUE', 'ACCEPT'), policy.replace('ports:', 'ports: 8443/tcp', 1),
                    policy+'    rule family="ipv6" port port="8443" protocol="tcp" accept\n'):
            with self.assertRaises(ValueError):firewall.validate_policy(bad)

    def test_named_service_cannot_hide_an_extra_port(self):
        firewall.validate_service('ssh', 'ssh\n  ports: 22/tcp\n  protocols:')
        with self.assertRaises(ValueError):
            firewall.validate_service('ssh', 'ssh\n  ports: 22/tcp 24443/tcp')
        with self.assertRaises(ValueError):
            firewall.validate_service('ssh', 'ssh\n  ports: 22/tcp\n  includes: cockpit')

if __name__ == '__main__':unittest.main()
