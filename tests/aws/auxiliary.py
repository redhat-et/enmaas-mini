#!/usr/bin/env python3
"""Auxiliary profiles must not weaken gateway or network defaults."""
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader('vm', str(ROOT/'scripts/aws/rhel-vm'))
spec = importlib.util.spec_from_loader(loader.name, loader)
vm = importlib.util.module_from_spec(spec)
loader.exec_module(vm)

class Auxiliary(unittest.TestCase):
    def test_registry_runs_share_ownership_tag_without_weakening_role_tags(self):
        run = 'enmaas-0123456789abcdef'
        for role, scenario in (('gateway', 'remote-gateway'), ('keycloak', 'keycloak'),
                               ('observability', 'observability')):
            prefix = run+'-'+role
            tags = vm.tags(prefix, scenario)
            mapped = {item['Key']: item['Value'] for item in tags}
            self.assertEqual(mapped['enmaas:run'], run)
            self.assertEqual(mapped['enmaas:purpose'], 'external-services-test')
            self.assertTrue(vm.owned_tags({'Tags': tags}, prefix, scenario))
            changed = [dict(item, Value='foreign') if item['Key']=='enmaas:run' else item for item in tags]
            self.assertFalse(vm.owned_tags({'Tags': changed}, prefix, scenario))
            missing = [item for item in tags if item['Key']!='enmaas:run']
            self.assertFalse(vm.owned_tags({'Tags': missing}, prefix, scenario))
        with self.assertRaises(ValueError):
            vm.tags(run+'-gateway', 'keycloak')
        self.assertNotIn('enmaas:run', {item['Key'] for item in vm.tags('legacy-gateway', 'remote-gateway')})


    def test_sizing_is_role_specific(self):
        for role, memory in [('keycloak', 8192), ('observability', 16384)]:
            machine = {'MemoryInfo': {'SizeInMiB': memory},
                       'ProcessorInfo': {'SupportedArchitectures': ['x86_64']}}
            vm.validate_machine(machine, 'x86_64', 'none', role)
            with self.assertRaises(ValueError):
                vm.validate_machine(machine, 'x86_64', 'none', 'remote-gateway')
            with self.assertRaises(ValueError):
                vm.validate_machine(machine, 'aarch64', 'none', role)
            machine['MemoryInfo']['SizeInMiB'] = memory - 1
            with self.assertRaises(ValueError):
                vm.validate_machine(machine, 'x86_64', 'none', role)

    def test_auxiliary_ingress_never_exposes_backends(self):
        for role in ('keycloak', 'observability'):
            rules = vm.ingress(role, '198.51.100.4/32')
            self.assertEqual({r['FromPort'] for r in rules}, {22, 8443})
            self.assertTrue(all(r['IpRanges'] == [{'CidrIp': '198.51.100.4/32'}] for r in rules))
            with self.assertRaises(ValueError):
                vm.ingress(role, '198.51.100.4/32', https_access='public')
            with self.assertRaises(ValueError):
                vm.ingress(role, '198.51.100.4/32', ssh_access='public')

if __name__ == '__main__':
    unittest.main()
