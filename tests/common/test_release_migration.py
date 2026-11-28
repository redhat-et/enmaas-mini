"""Release references and migration instructions must agree across entry points."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('pricetag_installer', ROOT / 'scripts/pricetag/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ReleaseMigrationTest(unittest.TestCase):
    def test_metadata_import_is_independent_of_gateway_test_fixture(self):
        result = subprocess.run([sys.executable, '-c', '''
import runpy, sys, types
sys.modules['gateway'] = types.ModuleType('gateway')
original_path = list(sys.path)
runpy.run_path(sys.argv[1])
assert sys.path == original_path
assert 'prepare' not in sys.modules
''', str(ROOT / 'tests/pricetag/published-images.py')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_image_check_rejects_wrong_architecture_and_revision(self):
        spec = importlib.util.spec_from_file_location('published_images', ROOT / 'tests/pricetag/published-images.py')
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
        for labels in ({'Labels': {'org.opencontainers.image.revision': 'expected'}},
                       {'Config': {'Labels': {'org.opencontainers.image.revision': 'expected'}}}):
            metadata = {'Os': 'linux', 'Architecture': 'amd64', **labels}
            images.verify_metadata(metadata, 'expected')
            with self.assertRaisesRegex(ValueError, 'revision'):
                images.verify_metadata(metadata, 'other')
            with self.assertRaisesRegex(ValueError, 'amd64'):
                images.verify_metadata({**metadata, 'Architecture': 'arm64'}, 'expected')

    def test_publisher_and_installer_use_the_same_repositories_and_pinned_sources(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/pricetag-images.yml').read_text())
        job = workflow['jobs']['build-test-publish']
        # This repository builds and publishes only the metering image; MaaS
        # is published by the external enmaas-api pipeline, and the workflow
        # pulls it straight from the installer's pin at run time.
        image = installer.PUBLISHED_METERING_IMAGE
        self.assertEqual(image.split('@')[0], job['env']['METERING_IMAGE'])
        # Publication must precede switching the installer to the new digest.
        # These pins can differ; published-images.py verifies the installer's
        # digest against its own source revision, not the next build target.
        self.assertRegex(installer.METERING_COMMIT, r'^[0-9a-f]{40}$')
        self.assertRegex(job['env']['METERING_REVISION'], r'^[0-9a-f]{40}$')
        self.assertRegex(image, r'@sha256:[0-9a-f]{64}$')
        self.assertIn(image, (ROOT / 'docs/quickstarts/remote-gateway-pricetag/manual.md').read_text())
        # The external MaaS image must still be a digest pin documented in the
        # manual and exercised by the workflow's component test.
        self.assertRegex(installer.PUBLISHED_MAAS_IMAGE, r'@sha256:[0-9a-f]{64}$')
        self.assertIn(installer.PUBLISHED_MAAS_IMAGE,
                      (ROOT / 'docs/quickstarts/remote-gateway-pricetag/manual.md').read_text())
        self.assertNotIn('MAAS_IMAGE', job['env'])
        test_step = next(step for step in job['steps'] if 'component-images.py' in str(step))
        # The workflow pins the digest visibly, compares it textually with
        # the installer constant, and runs the component test on exactly
        # that reference — no repository Python is imported to check the pin.
        # Assert on the raw run block: the dict repr escapes the quotes the
        # textual comparison is required to carry.
        run_block = test_step['run']
        self.assertIn(installer.PUBLISHED_MAAS_IMAGE, run_block)
        self.assertIn("grep -qF \"PUBLISHED_MAAS_IMAGE = '$maas'\"", run_block)
        self.assertIn('--maas-image "$maas"', run_block)
        publish = next(step for step in job['steps'] if step.get('name') == 'Publish tested metering image')
        for condition in (job['environment'], publish['if']):
            self.assertIn("github.repository == 'redhat-et/enmaas-mini'", condition)
            self.assertIn("github.ref == 'refs/heads/main'", condition)
            self.assertIn("github.event_name != 'pull_request'", condition)

    def test_aws_clone_enters_the_created_directory(self):
        source = (ROOT / 'docs/testing/aws-pricetag.md').read_text()
        self.assertRegex(source, r'git clone https://github.com/redhat-et/enmaas-mini.git\ncd enmaas-mini')

    def test_imported_issue_numbers_are_not_retargeted_to_the_new_repository(self):
        for path in (ROOT / 'docs').rglob('*.md'):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotRegex(path.read_text(),
                    r'https://github.com/redhat-et/enmaas-mini/(?:issues|pull)/(?:45|58|91|92|100|103|104)(?![0-9])')

    def test_bootc_is_explicitly_opt_in_and_remains_fork_isolated(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/bootc-images.yml').read_text())
        condition = workflow['jobs']['build']['if']
        self.assertIn("vars.BOOTC_IMAGE_BUILDS_ENABLED == 'true'", condition)
        self.assertIn("github.repository == 'redhat-et/enmaas-mini'", condition)
        self.assertIn('github.event.pull_request.head.repo.fork == false', condition)
