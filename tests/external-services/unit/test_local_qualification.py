"""CI orchestration must refuse existing fixtures and always clean its own pod."""
import contextlib
import importlib.util
import io
from pathlib import Path
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('local_qualification',Path(__file__).resolve().parents[1]/'local-qualification.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class Qualification(unittest.TestCase):
    def fixture(self, root):
        return SimpleNamespace(STATE=root/'new-fixture', POD='enmaas-external-ci',
                               IMAGES={'praxis':'pinned-praxis'}, write=Mock())

    def test_other_instances_and_existing_state_or_pods_are_never_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab=self.fixture(Path(tmp))
            for instance, state_exists, pod_status in [('other',False,1),('external-ci',True,1),
                                                       ('external-ci',False,0),('external-ci',False,125)]:
                if state_exists:lab.STATE.mkdir()
                with self.subTest(instance=instance,existing=state_exists,pod_status=pod_status), \
                     patch.dict(os.environ,ENMAAS_LAB_INSTANCE=instance), \
                     patch.object(runner.sys,'argv',['qualify']),patch.object(runner,'load_lab',return_value=lab), \
                     patch.object(runner.subprocess,'run',return_value=SimpleNamespace(returncode=pod_status)) as call, \
                     contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    runner.main()
                self.assertLessEqual(call.call_count,1)
                if call.called:self.assertEqual(call.call_args.args[0][:3],['podman','pod','exists'])
                if state_exists:lab.STATE.rmdir()

    def test_failed_probe_runs_owned_cleanup_and_never_marks_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab=self.fixture(Path(tmp));calls=[]
            def invoke(command,**kwargs):
                calls.append(command)
                if command[:3]==['podman','pod','exists']:return SimpleNamespace(returncode=1)
                self.assertGreater(kwargs['timeout'],0)
                if command[1].endswith('signals.py'):raise subprocess.CalledProcessError(1,command)
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ,ENMAAS_LAB_INSTANCE='external-ci'), \
                 patch.object(runner.sys,'argv',['qualify']),patch.object(runner,'load_lab',return_value=lab), \
                 patch.object(runner.subprocess,'run',side_effect=invoke), \
                 contextlib.redirect_stdout(io.StringIO()),self.assertRaises(subprocess.CalledProcessError):
                runner.main()
            self.assertEqual(calls[-1][-2:],[str(runner.HERE/'local.py'),'down'])
            lab.write.assert_not_called()

    def test_success_evidence_requires_successful_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            for fail_cleanup in (False, True):
                lab=self.fixture(Path(tmp));cleaned=[]
                def invoke(command, **kwargs):
                    if command[:3]==['podman','pod','exists']:
                        return SimpleNamespace(returncode=1)
                    if command[-2:]==[str(runner.HERE/'local.py'),'down']:
                        if fail_cleanup:raise subprocess.CalledProcessError(1,command)
                        cleaned.append(True)
                    return SimpleNamespace(returncode=0)
                lab.write.side_effect=lambda *args:self.assertEqual(cleaned,[True])
                with self.subTest(fail_cleanup=fail_cleanup), \
                     patch.dict(os.environ,ENMAAS_LAB_INSTANCE='external-ci'), \
                     patch.object(runner.sys,'argv',['qualify']),patch.object(runner,'load_lab',return_value=lab), \
                     patch.object(runner.subprocess,'run',side_effect=invoke),contextlib.redirect_stdout(io.StringIO()):
                    if fail_cleanup:
                        with self.assertRaises(subprocess.CalledProcessError):runner.main()
                        lab.write.assert_not_called()
                    else:
                        runner.main()
                        lab.write.assert_called_once()


if __name__=='__main__':unittest.main()
