"""Fresh runtime qualification owns only temporary containers, secrets and databases."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('persona_runtime',(Path(__file__).resolve().parents[1] / 'persona-runtime.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class Runtime(unittest.TestCase):
    def fixture(self,root,*,fail_probe=False,fail_cleanup=False):
        state=root/'state';state.mkdir(mode=0o700)
        (state/'credentials.json').write_text(json.dumps({'db':'private-password-sentinel'}))
        (state/'gateway-sso.env').write_text('PORT=8087\n')
        (state/'gateway-sso.json').write_text('{"listener":"127.0.0.1:28443"}')
        calls=[]
        def run(*args,**kwargs):
            calls.append((args,kwargs))
            if args[:2]==('pod','inspect'):
                return json.dumps([{'Labels':{'io.enmaas.test-lab':'unit'}}]).encode()
            if args[0]=='inspect':
                return json.dumps([{'Config':{'Labels':{'io.enmaas.test-lab':'unit'}},'Image':'sha256:synthetic'}]).encode()
            if args[0]=='run' and '--env-file' in args:
                env_path=Path(args[args.index('--env-file')+1])
                self.assertEqual(env_path.stat().st_mode&0o777,0o600)
            if args[:2]==('run','--rm'):
                if fail_probe:raise RuntimeError('probe failed')
                return b'PASS: isolated fixture\n'
            if fail_cleanup and args[0]=='rm':raise RuntimeError('cleanup failure')
            return b''
        lab=SimpleNamespace(POD='enmaas-unit',INSTANCE='unit',LABEL='io.enmaas.test-lab=unit',STATE=state,secret_name=lambda name:'unit-'+name,port=lambda role:28443)
        return SimpleNamespace(lab=lab,run=run,prepare=SimpleNamespace(PYTHON='python-pinned'),maas_environment=lambda:{}),calls

    def test_fresh_databases_and_no_published_ports_cleanup_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            sso,calls=self.fixture(Path(tmp))
            with patch.object(p,'load',return_value=sso),contextlib.redirect_stdout(io.StringIO()) as output:p.main()
        self.assertIn('PASS',output.getvalue())
        command_args=[args for args,_ in calls]
        self.assertNotIn('private-password-sentinel',repr(command_args))
        launches=[args for args in command_args if args[0]=='run']
        self.assertEqual(len(launches),4)
        self.assertTrue(all('--publish' not in args and '--privileged' not in args for args in launches))
        self.assertTrue(all('--read-only' in args and 'no-new-privileges' in args for args in launches))
        created=[args[-1] for args in command_args if len(args)>2 and args[2]=='createdb']
        dropped=[args[-1] for args in command_args if len(args)>2 and args[2]=='dropdb']
        self.assertEqual(len(created),2);self.assertEqual(set(created),set(dropped))
        self.assertTrue(all(name.startswith('persona_') for name in created))
        removals=[args[-1] for args in command_args if args[0]=='rm']
        self.assertEqual(len(removals),4);self.assertTrue(all(name.startswith('enmaas-unit-persona-') for name in removals))

    def test_probe_failure_still_drops_only_owned_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            sso,calls=self.fixture(Path(tmp),fail_probe=True)
            with patch.object(p,'load',return_value=sso),self.assertRaises(RuntimeError):p.main()
        self.assertEqual(sum(1 for args,_ in calls if len(args)>2 and args[2]=='dropdb'),2)

    def test_cleanup_continues_after_one_resource_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            sso,calls=self.fixture(Path(tmp),fail_cleanup=True)
            with patch.object(p,'load',return_value=sso),contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(ValueError,'cleanup incomplete'):p.main()
        self.assertEqual(sum(1 for args,_ in calls if len(args)>2 and args[2]=='dropdb'),2)

    def test_foreign_pod_refused_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sso,_=self.fixture(Path(tmp));calls=[]
            def run(*args):calls.append(args);return b'[{"Labels":{}}]'
            sso.run=run
            with patch.object(p,'load',return_value=sso),self.assertRaisesRegex(ValueError,'Foreign'):p.main()
        self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main()
