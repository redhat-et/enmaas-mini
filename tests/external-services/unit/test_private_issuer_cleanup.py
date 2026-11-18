"""Timeouts must not leave disposable clients holding issuer credentials."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('private_issuer',(Path(__file__).resolve().parents[1] / 'private-issuer-runtime.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)


class Cleanup(unittest.TestCase):
    def test_population_failure_reports_only_bounded_progress(self):
        progress={'stage':'create','created':4095,'verified':4095,'deleted':4095,'errno':28}
        with self.assertRaisesRegex(RuntimeError,'4095'):
            p.report_population_failure(json.dumps(progress))
        for output in ('secret-sentinel','[]',json.dumps(dict(progress,password='secret-sentinel')),
                       json.dumps(dict(progress,stage='secret-sentinel')),json.dumps(dict(progress,created=-1)),
                       json.dumps(dict(progress,errno='secret-sentinel'))):
            with self.assertRaises(RuntimeError) as caught:p.report_population_failure(output)
            self.assertNotIn('secret-sentinel',str(caught.exception))

    def test_population_bounds_fail_before_runtime_operations(self):
        for count in (0,10001,True,'20'):
            with patch.object(p,'command') as command,self.assertRaises(ValueError):
                p.main(keycloak=True,provisioning=True,population_count=count)
            command.assert_not_called()
        with patch.object(p,'command') as command,self.assertRaises(ValueError):
            p.main(population_count=100)
        command.assert_not_called()


    def test_success_failure_and_timeout_remove_the_exact_ephemeral_client(self):
        for outcome in (Mock(returncode=0,stdout='PASS'),Mock(returncode=1),subprocess.TimeoutExpired('podman',60)):
            with patch.object(p.subprocess,'run',side_effect=[outcome,Mock(returncode=0)]) as run:
                if isinstance(outcome,Exception) or outcome.returncode:
                    with self.assertRaises((RuntimeError,subprocess.TimeoutExpired)):
                        p.command('run','--rm','--interactive','image','python3',input='test')
                else:p.command('run','--rm','--interactive','image','python3',input='test')
            invocation=run.call_args_list[0].args[0]
            self.assertIn('--name',invocation)
            name=invocation[invocation.index('--name')+1]
            self.assertTrue(name.startswith('enmaas-issuer-client-'))
            self.assertEqual(run.call_args_list[1].args[0],['podman','rm','--force','--ignore',name])

    def test_server_operations_are_not_removed_and_failed_cleanup_is_visible(self):
        with patch.object(p.subprocess,'run',return_value=Mock(returncode=0)) as run:
            p.command('run','--detach','--name','server','image')
            self.assertEqual(run.call_count,1)
        with patch.object(p.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=1)]):
            with self.assertRaisesRegex(RuntimeError,'cleanup'):
                p.command('run','--rm','image')


if __name__=='__main__':unittest.main()
