"""Local population probe is restricted to the selected disposable pod."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('population_runtime',(Path(__file__).resolve().parents[1] / 'population-runtime.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)


class Runtime(unittest.TestCase):
    def test_failure_details_only_expose_fixed_probe_codes(self):
        error=subprocess.CalledProcessError(1,['client'],stderr=b'SECRET\nPROBE_FAILURE stage=clear-keys reason=other status=0\n')
        self.assertEqual(p.failure_details(error),'stage=clear-keys reason=other status=0')
        for error in (RuntimeError('SECRET'),subprocess.TimeoutExpired(['client'],1),
                      subprocess.CalledProcessError(1,['client'],stderr=b'PROBE_FAILURE stage=SECRET reason=other status=0'),
                      subprocess.CalledProcessError(1,['client'],stderr=b'PROBE_FAILURE stage=clear-keys reason=SECRET status=0'),
                      subprocess.CalledProcessError(1,['client'],stderr=b'PROBE_FAILURE stage=clear-keys reason=other status=SECRET'),
                      subprocess.CalledProcessError(1,['client'],stderr=None)):
            self.assertEqual(p.failure_details(error),'details suppressed')

    def test_runner_forwards_bounded_execution_timeout(self):
        with patch.object(p.sso.subprocess,'run',return_value=SimpleNamespace(stdout=b'PASS')) as run:
            self.assertEqual(p.sso.run('version',timeout=600),b'PASS')
        self.assertEqual(run.call_args.kwargs['timeout'],600)
        self.assertTrue(run.call_args.kwargs['check'])

    def test_population_size_is_bounded_before_contacting_podman(self):
        for count in (0,1,10001,True,'20'):
            with self.subTest(count=count),patch.object(p.sso,'run') as run:
                with self.assertRaises(ValueError):p.main(count)
                run.assert_not_called()

    def test_inference_refuses_nonlocal_or_changed_mock_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def config(endpoints):
                (root/'gateway-sso.json').write_text(json.dumps({'filter_chains':[{'filters':[{'clusters':[{'endpoints':endpoints}]}]}]}))
            with patch.object(p.sso.lab,'STATE',root):
                for endpoints in ([],['provider.example:443'],[None]):
                    config(endpoints)
                    with self.assertRaises(ValueError):p.mock_model()
                config(['127.0.0.1:8888']);self.assertEqual(p.mock_model(),p.sso.prepare.mock_inputs()[1][0]['id'])
                (root/'mock-profiles.json').write_text('{}')
                with self.assertRaises(ValueError):p.mock_model()
                from mock_profiles import configuration
                (root/'mock-profiles.json').write_text(json.dumps(configuration()))
                self.assertEqual(p.mock_model(),'openai/gpt-5.4-mini')

    def test_foreign_pod_refused_before_client_launch(self):
        with patch.object(p.sso,'run',return_value=b'[{"Labels":{}}]') as run:
            with self.assertRaises(ValueError):p.main()
            self.assertEqual(run.call_count,1)

    def test_failed_probe_removes_its_temporary_management_secret_and_client(self):
        info=json.dumps([{'Labels':{'io.enmaas.test-lab':p.sso.lab.INSTANCE}}]).encode()
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);(state/'gateway-sso.env').write_text('MANAGEMENT_API_SECRET=synthetic-management-secret\n')
            with patch.object(p,'mock_model',return_value='mock/fixture'),patch.object(p.sso.lab,'STATE',state),patch.object(p.sso,'run',side_effect=[info,b'',RuntimeError('probe failed'),b'',b'']) as run:
                with self.assertRaises(RuntimeError):p.main()
        create=run.call_args_list[1].args
        execute=run.call_args_list[2].args
        client=execute[execute.index('--name')+1]
        self.assertEqual(run.call_args_list[-2].args,('rm','--force','--ignore',client))
        self.assertEqual(run.call_args_list[-1].args,('secret','rm',create[-2]))

    def test_cleanup_attempts_both_resources_after_failure_or_timeout(self):
        info=json.dumps([{'Labels':{'io.enmaas.test-lab':p.sso.lab.INSTANCE}}]).encode()
        for failures in ([info,b'',b'PASS',RuntimeError('SECRET'),b''],
                         [info,b'',b'PASS',b'',subprocess.TimeoutExpired(['secret'],60)],
                         [info,RuntimeError('uncertain creation'),b'',b'']):
            with tempfile.TemporaryDirectory() as tmp:
                state=Path(tmp);(state/'gateway-sso.env').write_text('MANAGEMENT_API_SECRET=synthetic-management-secret\n')
                with patch.object(p,'mock_model',return_value='mock/fixture'),patch.object(p.sso.lab,'STATE',state), \
                     patch.object(p.sso,'run',side_effect=failures) as run:
                    with self.assertRaises(Exception) as error:p.main()
            self.assertNotIn('SECRET',str(error.exception))
            self.assertEqual(run.call_args_list[-2].args[:3],('rm','--force','--ignore'))
            self.assertEqual(run.call_args_list[-1].args[:2],('secret','rm'))
            self.assertEqual(run.call_args_list[-2].kwargs['timeout'],60)
            self.assertEqual(run.call_args_list[-1].kwargs['timeout'],60)

    def test_only_reviewed_code_and_selected_secret_enter_readonly_client(self):
        info=json.dumps([{'Labels':{'io.enmaas.test-lab':p.sso.lab.INSTANCE}}]).encode()
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);(state/'gateway-sso.env').write_text('MANAGEMENT_API_SECRET=synthetic-management-secret\n')
            with patch.object(p,'mock_model',return_value='mock/fixture'),patch.object(p.sso.lab,'STATE',state),patch.object(p.sso,'run',side_effect=[info,b'',b'PASS\n',b'',b'']) as run,contextlib.redirect_stdout(io.StringIO()) as out:
                p.main(20)
        invocation=run.call_args_list[2]
        args=invocation.args
        self.assertIn('PROBE_POPULATION_COUNT=20',args)
        self.assertEqual(args[args.index('--memory')+1],'512m')
        self.assertIn('/tmp:rw,noexec,nosuid,size=256m',args)
        self.assertEqual(invocation.kwargs['timeout'],600)
        self.assertIn('--pull=never',args);self.assertIn('--read-only',args)
        self.assertIn('no-new-privileges',args);self.assertNotIn('--privileged',args)
        self.assertNotIn('--publish',args);self.assertNotIn('--volume',args)
        self.assertEqual(args.count('--secret'),3)
        self.assertIn(p.sso.lab.secret_name('ca.pem')+',target=/fixture/ca.pem,mode=0444',args)
        self.assertIn(p.sso.lab.secret_name('gateway-sso-partner')+',target=/fixture/partner-secret,mode=0400',args)
        with tarfile.open(fileobj=io.BytesIO(invocation.kwargs['input'])) as archive:
            self.assertEqual(set(archive.getnames()),set(p.FILES))
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
        self.assertEqual(out.getvalue(),'PASS\n')
        self.assertNotIn('synthetic-management-secret',repr(args))
        self.assertEqual(run.call_args_list[-2].args[:3],('rm','--force','--ignore'))
        self.assertEqual(run.call_args_list[-1].args[:2],('secret','rm'))


if __name__=='__main__':unittest.main()
