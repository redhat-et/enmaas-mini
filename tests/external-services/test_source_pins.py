"""Prevent the CI source build from silently qualifying a different installer."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


def literal_assignment(path, name):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'Missing source pin: {name}')


class SourcePins(unittest.TestCase):
    def test_metering_ci_and_builder_match_installer_source_candidate(self):
        installer = literal_assignment(ROOT/'scripts/pricetag/install.py', 'SOURCE_METERING_COMMIT')
        builder = literal_assignment(ROOT/'scripts/pricetag/build-metering', 'REVISION')
        workflow = (ROOT/'.github/workflows/pricetag-demo.yml').read_text()
        pins = re.findall(r'^\s+metering=([0-9a-f]{40})$', workflow, re.MULTILINE)
        self.assertEqual(pins, [installer])
        self.assertEqual(builder, installer)


if __name__ == '__main__':
    unittest.main()
