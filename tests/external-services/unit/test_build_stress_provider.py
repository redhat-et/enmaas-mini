"""The isolated mock must run unprivileged even when built with a private umask."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('build_stress_provider',Path(__file__).resolve().parents[1]/'build-stress-provider.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class MockImageInputs(unittest.TestCase):
    def test_allowlisted_inputs_and_nonroot_readability(self):
        files,image=builder.inputs()
        self.assertEqual(set(files),{'Containerfile','requirements.txt','provider.py','async_provider.py','stream_payload.py'})
        recipe=files['Containerfile'].decode()
        self.assertIn('USER 65532:65532',recipe)
        copies=[line for line in recipe.splitlines() if line.startswith('COPY ')]
        self.assertEqual(len(copies),4)
        self.assertTrue(all(line.startswith('COPY --chmod=0644 ') for line in copies))
        self.assertRegex(image,r'^localhost/enmaas-stress-provider:[a-f0-9]{16}$')
