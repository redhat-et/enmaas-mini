#!/usr/bin/env python3
"""Run mocked AWS and coordinator contracts with branch coverage, without AWS access."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import coverage

ROOT = Path(__file__).resolve().parents[2]
TESTS = (
    'tests/pricetag/install.py',
    'tests/pricetag/install-orchestration.py',
    'tests/aws/plan.py',
    'tests/aws/environment.py',
    'tests/aws/auxiliary.py',
    'tests/aws/vm-boundaries.py',
    'tests/external-services/unit/test_deployments.py',
    'tests/external-services/unit/test_dns.py',
    'tests/external-services/unit/test_rds.py',
    'tests/external-services/unit/test_network.py',
    'tests/external-services/unit/test_cleanup_inventory.py',
    'tests/external-services/unit/test_cleanup_instances.py',
    'tests/external-services/unit/test_cleanup_keys.py',
    'tests/external-services/unit/test_cleanup_groups.py',
    'tests/external-services/unit/test_aws_discover.py',
    'tests/external-services/unit/test_zone_select.py',
    'tests/external-services/unit/test_lab.py',
    'tests/external-services/unit/test_vpc_plan.py',
    'tests/external-services/unit/test_vpc_create.py',
    'tests/external-services/unit/test_vpc_inspect.py',
    'tests/external-services/unit/test_network_policy.py',
    'tests/external-services/unit/test_vm_policy.py',
    'tests/external-services/unit/test_policy_pack.py',
    'tests/external-services/unit/test_operator_bootstrap.py',
    'tests/external-services/unit/test_operator_profile.py',
    'tests/external-services/unit/test_infrastructure.py',
    'tests/external-services/unit/test_infrastructure_info.py',
    'tests/external-services/unit/test_transfer_client.py',
    'tests/external-services/unit/test_keycloak_password.py',
    'tests/external-services/unit/test_user_population.py',
    'tests/external-services/unit/test_populate_keycloak.py',
    'tests/external-services/unit/test_import_population.py',
    'tests/external-services/unit/test_services.py',
    'tests/external-services/unit/test_personas.py',
    'tests/external-services/unit/test_population_key.py',
)
# Keep this gate aligned with coverage.ini when adding orchestration modules.
FULL_COVERAGE = (
    'scripts/pricetag/install.py',
    'scripts/aws/rhel-vm',
    'scripts/aws/environment.py',
    'tests/external-services/network.py',
    'tests/external-services/deployments.py',
    'tests/external-services/dns.py',
    'tests/external-services/zone-select.py',
    'tests/external-services/rds.py',
    'tests/external-services/cleanup-inventory.py',
    'tests/external-services/cleanup-instances.py',
    'tests/external-services/cleanup-keys.py',
    'tests/external-services/cleanup-groups.py',
    'tests/external-services/vpc-plan.py',
    'tests/external-services/vpc-create.py',
    'tests/external-services/vpc-inspect.py',
    'tests/external-services/network-policy.py',
    'tests/external-services/vm-policy.py',
    'tests/external-services/policy-pack.py',
    'tests/external-services/operator-bootstrap.py',
    'tests/external-services/operator-profile.py',
    'tests/external-services/infrastructure.py',
    'tests/external-services/infrastructure-info.py',
    'tests/external-services/transfer-client.py',
    'tests/external-services/keycloak-password.py',
    'tests/external-services/user-population.py',
    'tests/external-services/populate-keycloak.py',
    'tests/external-services/import-population.py',
    'tests/external-services/services.py',
    'tests/external-services/personas.py',
    'tests/external-services/population-key.py',
)


def main():
    os.chdir(ROOT)
    with tempfile.TemporaryDirectory(prefix='enmaas-mocked-aws-') as temporary:
        home = Path(temporary)
        cli = home/'aws'
        cli.write_text('#!/bin/sh\nprintf "Unexpected unmocked AWS CLI invocation\\n" >&2\nexit 97\n')
        cli.chmod(0o700)
        # Never inherit a developer's credentials, profile or SDK configuration.
        environment = {key: value for key, value in os.environ.items() if not key.startswith('AWS_')}
        environment.update(HOME=str(home), AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null',
                           AWS_EC2_METADATA_DISABLED='true', AWS_ACCESS_KEY_ID='mock-access-not-valid',
                           AWS_SECRET_ACCESS_KEY='mock-secret-not-valid', AWS_SESSION_TOKEN='mock-session-not-valid',
                           AWS_DEFAULT_REGION='eu-central-1', COVERAGE_FILE=str(ROOT/'.state/coverage/aws'), PATH=os.pathsep.join((str(home),str(Path(sys.executable).parent),os.environ['PATH'])))
        cov = coverage.Coverage(config_file=str(ROOT/'tests/aws/coverage.ini'), data_file=str(ROOT/'.state/coverage/aws'))
        cov.erase()
        cov.start()
        # Coverage config is injected at start; retain it when isolating the child environment.
        environment.update({key: value for key, value in os.environ.items() if key.startswith('COVERAGE_')})
        try:
            with patch.dict(os.environ, environment, clear=True), patch.object(
                    socket.socket, 'connect', side_effect=AssertionError('Network access is forbidden in mocked tests')), patch.object(
                    socket.socket, 'connect_ex', side_effect=AssertionError('Network access is forbidden in mocked tests')):
                suite = unittest.TestSuite()
                for index, name in enumerate(TESTS):
                    spec = importlib.util.spec_from_file_location('aws_contract_'+str(index), ROOT/name)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
                result = unittest.TextTestRunner(verbosity=1).run(suite)
        finally:
            cov.stop()
            cov.save()
        cov.combine()
        cov.save()
        cov.report()
        cov.json_report()
        cov.html_report()
        if not result.wasSuccessful():
            return 1
        report = json.loads((ROOT/'.state/coverage/aws.json').read_text())
        for path in FULL_COVERAGE:
            summary = report['files'][path]['summary']
            if summary['missing_lines'] or summary['missing_branches']:
                print('Full statement/branch coverage required: '+path, file=sys.stderr)
                return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
