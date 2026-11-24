#!/usr/bin/env python3
"""Negative tests: teardown must fail closed without a matching deploy manifest."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEARDOWN = ROOT / "scripts/laptop/teardown"


def run(deploy_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TEARDOWN), "--purge-volumes", "--dry-run",
         "--yes", "--deploy-dir", str(deploy_dir)],
        capture_output=True, text=True, cwd="/tmp",
    )


def main() -> int:
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # 1) no manifest -> fail closed
        p = run(d)
        if p.returncode == 0:
            print("FAIL: teardown allowed purge without manifest")
            failures += 1
        else:
            print("PASS: no manifest refused")

        # 2) mismatched manifest -> fail closed
        (d / ".deploy-manifest.json").write_text(json.dumps({
            "deploy_dir": "/some/other/dir",
            "volumes": ["pricetag-data"],
            "networks": ["pricetag-private", "pricetag-mock"],
            "secrets": ["pricetag-m2m", "pricetag-mock-ui-v1"],
            "containers": ["pricetag-db"],
            "engine": "podman",
        }))
        p = run(d)
        if p.returncode == 0:
            print("FAIL: mismatched manifest allowed")
            failures += 1
        else:
            print("PASS: mismatched manifest refused")

        # 3) valid manifest -> dry-run proceeds
        (d / ".deploy-manifest.json").write_text(json.dumps({
            "deploy_dir": str(d),
            "compose_file": str(d / "compose.laptop.yaml"),
            "volumes": ["pricetag-data"],
            "networks": ["pricetag-private", "pricetag-mock"],
            "secrets": ["pricetag-m2m", "pricetag-mock-ui-v1"],
            "containers": ["pricetag-db"],
            "engine": "podman",
        }))
        p = run(d)
        if p.returncode != 0:
            print("FAIL: valid manifest dry-run failed:", p.stderr.strip()[:200])
            failures += 1
        else:
            print("PASS: valid manifest dry-run")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
