"""The CI entry point must never overwrite an existing local fixture."""
import contextlib
import importlib.util
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('ci_runtime',(Path(__file__).resolve().parents[1] / 'ci-runtime.py'))
ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)

class FreshFixture(unittest.TestCase):
    def test_existing_pod_or_failed_probe_does_not_trigger_cleanup(self):
        for code in (0,2,125):
            with patch.dict(ci.os.environ,{'ENMAAS_LAB_INSTANCE':'external-ci'}),patch('sys.argv',['ci-runtime.py']),patch.object(ci.Path,'exists',return_value=False),patch.object(ci,'load',return_value=SimpleNamespace(POD='enmaas-external-ci')),patch.object(ci.subprocess,'run',return_value=SimpleNamespace(returncode=code)),patch.object(ci,'qualify') as qualify,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):ci.main()
                qualify.assert_not_called()

    def test_existing_volume_is_not_treated_as_fresh_state(self):
        lab=SimpleNamespace(POD='enmaas-external-ci')
        with patch.dict(ci.os.environ,{'ENMAAS_LAB_INSTANCE':'external-ci'}),patch('sys.argv',['ci-runtime.py']),patch.object(ci.Path,'exists',return_value=False),patch.object(ci,'load',return_value=lab),patch.object(ci.subprocess,'run',side_effect=[SimpleNamespace(returncode=1),SimpleNamespace(returncode=0)]) as command,patch.object(ci,'qualify') as qualify,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):ci.main()
            qualify.assert_not_called()
            self.assertIn('volume',command.call_args.args[0])

    def test_unique_ci_instance_uses_its_own_state_and_empty_volumes(self):
        instance='external-ci-abcd1234';lab=SimpleNamespace(POD='enmaas-'+instance,STATE=Path('/tmp/fixture'))
        with patch.dict(ci.os.environ,{'ENMAAS_LAB_INSTANCE':instance}),patch('sys.argv',['ci-runtime.py']),patch.object(ci.Path,'exists',return_value=False),patch.object(ci,'load',return_value=lab),patch.object(ci.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as command,patch.object(ci,'qualify') as qualify:
            ci.main()
            qualify.assert_called_once()
            self.assertEqual(command.call_count,6)
            self.assertTrue(all('enmaas-'+instance in call.args[0][-1] for call in command.call_args_list))

    def test_failed_stage_cleans_owned_fixture_without_success_evidence(self):
        lab=Mock(IMAGES={'praxis':'test-image'})
        runner=Mock()
        runner.run_check.side_effect=[RuntimeError('stage failure'),None]
        with patch.object(ci,'load',return_value=runner):
            with self.assertRaisesRegex(RuntimeError,'stage failure'):
                ci.qualify(lab,SimpleNamespace(duration=65),{})
        self.assertEqual(runner.run_check.call_args.args[:2],(ci.HERE/'local.py',['down']))
        lab.write.assert_not_called()

    def test_browser_organization_checks_have_an_active_revoked_key_team_fixture(self):
        # Workload cleanup retires its accounts. The organization table intentionally
        # lists active people, so seed the existing display-only team before reads.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);report=root/'lab-run-example';report.mkdir()
            (report/'summary.json').write_text(json.dumps({'phases':[
                {'started_utc':'2026-11-15T00:00:00+00:00',
                 'ended_utc':'2026-11-15T00:06:00+00:00'}]}))
            lab=Mock(IMAGES={'praxis':'test-image'},STATE=root)
            runner=Mock()
            builder=SimpleNamespace(inputs=lambda: ([], 'test-generator'))
            def load(file,name):
                return runner if file=='security-runtime.py' else builder
            commands=[]
            with patch.object(ci,'load',side_effect=load),patch.object(ci.importlib.util,'module_from_spec',return_value=builder),patch.object(ci.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=Mock())),patch.object(ci.subprocess,'run',side_effect=lambda command,**kw:commands.append(command)):
                ci.qualify(lab,SimpleNamespace(duration=65,metering_image='test-metering',maas_image='test-maas'),{})
            scripts=[Path(command[1]).name for command in commands]
            self.assertIn('alice-team.cjs',scripts)
            self.assertLess(scripts.index('alice-team.cjs'),scripts.index('organization-dashboard.cjs'))
            self.assertEqual(commands[scripts.index('alice-team.cjs')][2:],[str(root),str(report),'--apply'])
            self.assertEqual(runner.run_check.call_args.args[:2],(ci.HERE/'local.py',['down']))
            lab.write.assert_called_once()

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
