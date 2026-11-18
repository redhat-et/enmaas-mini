"""Tests for documentation staging."""

from __future__ import annotations

import shutil
import unittest

import stage


class StageTests(unittest.TestCase):
    def staged_paths(self, name: str, pattern: str) -> set[str]:
        destination = stage.STAGE / name
        try:
            stage.stage_docs(destination)
            return {
                path.relative_to(destination).as_posix()
                for path in destination.rglob(pattern)
            }
        finally:
            shutil.rmtree(destination)

    def test_published_testing_guides_are_staged(self):
        staged_paths = self.staged_paths("test-stage", "*.md")
        for path in (
            "docs/testing/README.md",
            "docs/testing/aws.md",
            "docs/testing/aws-operations.md",
            "docs/testing/mocked-provider.md",
            "docs/testing/pricetag.md",
            "docs/testing/aws-pricetag.md",
            "docs/testing/pricetag-perf.md",
            "docs/testing/external-services/README.md",
            "docs/testing/external-services/topology.md",
            "docs/testing/external-services/security-acceptance.md",
            "docs/testing/external-services/providers.md",
        ):
            self.assertIn(path, staged_paths)

    def test_removed_legacy_documents_are_not_staged(self):
        staged_paths = self.staged_paths("test-legacy-stage", "*.md")
        self.assertNotIn("docs/roadmap.md", staged_paths)
        self.assertNotIn("docs/testing/compatibility.md", staged_paths)
        self.assertNotIn("docs/testing/compatibility.md", staged_paths)

    def test_component_readmes_are_staged(self):
        staged_paths = self.staged_paths("test-component-stage", "*.md")
        self.assertIn("bootc/README.md", staged_paths)
        self.assertIn("bootc/VLLM.md", staged_paths)


if __name__ == "__main__":
    unittest.main()
