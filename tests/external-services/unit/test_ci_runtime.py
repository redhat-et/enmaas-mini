"""The CI entry point must never overwrite an existing local fixture."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('ci_runtime',(Path(__file__).resolve().parents[1] / 'ci-runtime.py'))
ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)

class FreshFixture(unittest.TestCase):
    def test_wrong_instance_cannot_start_containers(self):
        with patch.dict(ci.os.environ,{'ENMAAS_LAB_INSTANCE':'external-demo'}),patch('sys.argv',['ci-runtime.py']),patch.object(ci.subprocess,'run') as command,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):ci.main()
            command.assert_not_called()

    def test_existing_ci_fixture_is_not_reset(self):
        with patch.dict(ci.os.environ,{'ENMAAS_LAB_INSTANCE':'external-ci'}),patch('sys.argv',['ci-runtime.py']),patch.object(ci.Path,'exists',return_value=True),patch.object(ci.subprocess,'run') as command,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):ci.main()
            command.assert_not_called()

class EvidenceWindow(unittest.TestCase):
    def test_browser_window_covers_the_actual_run(self):
        phases=[{'started_utc':'2026-11-15T00:00:00+00:00','ended_utc':'2026-11-15T00:06:00+00:00'},
                {'started_utc':'2026-11-15T00:06:01+00:00','ended_utc':'2026-11-15T00:12:00+00:00'}]
        end,minutes=ci.evidence_window(phases)
        self.assertEqual(minutes,12)
        self.assertEqual(end,int(ci.datetime.datetime.fromisoformat(phases[-1]['ended_utc']).timestamp())-30)
        with self.assertRaises(ValueError):ci.evidence_window([])

if __name__=='__main__':unittest.main()
