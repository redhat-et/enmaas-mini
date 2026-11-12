#!/usr/bin/env python3
"""Cloud-free lab contract and provenance checks."""
import hashlib
import json
from pathlib import Path
import subprocess
import unittest

HERE = Path(__file__).resolve().parent

class Lab(unittest.TestCase):
    def test_preserved_dashboard_provenance(self):
        root = HERE/'perses'
        manifest = json.loads((root/'provenance.json').read_text())
        for name, digest in manifest['files'].items():
            payload = (root/name).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
            self.assertIn(json.loads(payload)['kind'], ('Dashboard', 'Project'))
        self.assertEqual(len(manifest['revision']), 40)

    def test_traffic_plan_is_mocked_bounded_and_has_an_unqualified_runner(self):
        plan = json.loads((HERE/'traffic-patterns.json').read_text())
        self.assertIs(plan['mock_only'], True)
        self.assertEqual(plan['phase_seconds'], 600)
        self.assertEqual(len(plan['phases']), 6)
        for phase in plan['phases']:
            self.assertLessEqual(phase['users'] * phase['streams_per_user'], 512)
        self.assertEqual(plan['runner'], 'tests/external-services/benchmark.py')
        self.assertTrue((HERE.parents[1]/plan['runner']).is_file())
        self.assertIn('RHEL execution pending', plan['runner_status'])

    def test_source_has_no_cloud_side_effect_and_plan_failure_blocks_launch(self):
        helper = str(HERE/'aws-lab.sh')
        result = subprocess.run(['bash', '-c', '''
set -eu
source "$1"
AWS_TEST_REPO=/reviewed
aws_test_plan() { printf 'plan %s\\n' "$1"; return 1; }
aws_test_deploy() { printf 'UNEXPECTED LAUNCH\\n'; }
if aws_lab_deploy; then exit 19; fi
''', 'test', helper], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'plan gateway\n')

    def test_deploy_keeps_three_separate_launches(self):
        result = subprocess.run(['bash', '-c', '''
set -eu
source "$1"
AWS_TEST_REPO=/reviewed
aws_test_plan() { :; }
aws_test_deploy() { printf '%s\\n' "$1"; }
aws_lab_deploy
''', 'test', str(HERE/'aws-lab.sh')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['gateway', 'keycloak', 'observability'])

    def test_info_exports_public_and_private_addresses(self):
        result=subprocess.run(['bash','-c','''
set -eu
source "$1"
AWS_TEST_REPO=/reviewed RUN_PREFIX=test REGION=example SSH_KEY=/test/key
jq() { printf 'i-test\\n'; }
aws() {
  case "$*" in
    *describe-instances*) printf '172.31.0.10\\n' ;;
    *wait*) : ;;
    *) return 99 ;;
  esac
}
aws_test_verify() { RHEL_HOST=ec2-user@198.51.100.10; }
aws_lab_info
[[ "$GATEWAY_HOST_IP" == 198.51.100.10 ]]
[[ "$KEYCLOAK_PRIVATE_IP" == 172.31.0.10 ]]
[[ "$OBSERVABILITY_PRIVATE_IP" == 172.31.0.10 ]]
''','test',str(HERE/'aws-lab.sh')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        for role in ('GATEWAY','KEYCLOAK','OBSERVABILITY'):
            self.assertIn(f'export {role}_HOST_IP=198.51.100.10',result.stdout)
            self.assertIn(f'export {role}_PRIVATE_IP=172.31.0.10',result.stdout)

if __name__ == '__main__':
    unittest.main()
