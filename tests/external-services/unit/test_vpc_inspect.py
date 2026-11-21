"""Offline journal inspection is structural evidence, never deletion authorization."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

spec=importlib.util.spec_from_file_location('vpc_inspect',(Path(__file__).resolve().parents[1] / 'vpc-inspect.py'))
audit=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class Inspection(unittest.TestCase):
    def test_cli_is_offline_and_diagnostics_do_not_echo_bad_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest,events=self.fixture(root/'network')
            manifest_path=audit.registry.save(root/'registry',manifest)
            command=[sys.executable,str((Path(__file__).resolve().parents[1] / 'vpc-inspect.py')),
                     '--manifest',str(manifest_path),'--journal',str(root/'network')]
            result=subprocess.run(command,capture_output=True,text=True,check=True)
            self.assertFalse(json.loads(result.stdout)['live_verified'])
            (root/'network/0001.json').write_text('secret invalid json')
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('inspection stopped',result.stderr)
            self.assertNotIn('secret',result.stderr)

    def fixture(self,root):
        manifest=audit.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')
        events=[{'event':'plan','manifest':manifest,'plan':{'deployment':manifest['id']}},
                {'event':'intent','action':'create-vpc','options':{'CidrBlock':'10.42.0.0/20'}},
                {'event':'result','action':'create-vpc','id':'vpc-12345678'}]
        root.mkdir(mode=0o700)
        self.write(root,events)
        return manifest,events

    def write(self,root,events):
        for path in root.glob('*.json'):path.unlink()
        for index,event in enumerate(events):
            path=root/(str(index).zfill(4)+'.json');path.write_text(json.dumps(event));path.chmod(0o600)

    def test_confirmed_resource_and_uncertain_operation_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'network';manifest,events=self.fixture(root)
            report=audit.inspect(root,manifest)
            self.assertEqual(report['state'],'interrupted-between-operations')
            self.assertEqual(report['confirmed_resources'],[{'action':'create-vpc','id':'vpc-12345678'}])
            self.assertFalse(report['deletion_authorized'])
            self.assertFalse(report['live_verified'])
            events.append({'event':'intent','action':'modify-vpc-attribute','options':{'VpcId':'vpc-12345678','EnableDnsSupport':{'Value':True}}})
            self.write(root,events)
            report=audit.inspect(root,manifest)
            self.assertEqual(report['state'],'uncertain-operation')
            self.assertEqual(report['pending_operation'],{'action':events[-1]['action']})
            events.append({'event':'result','action':'modify-vpc-attribute','id':None})
            events.append({'event':'complete','resources':{'vpc':'vpc-12345678'},'runtime_verified':False})
            self.write(root,events)
            self.assertEqual(audit.inspect(root,manifest)['state'],'creation-recorded-complete')

    def test_wrong_manifest_unsafe_files_and_noncontiguous_names_fail(self):
        for case in ('manifest','directory-mode','file-mode','symlink','missing','extra','empty','invalid-json'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'network';manifest,events=self.fixture(root)
                if case=='manifest':manifest['account']='999999999999'
                if case=='directory-mode':root.chmod(0o755)
                if case=='file-mode':(root/'0001.json').chmod(0o644)
                if case=='symlink':
                    (root/'0001.json').unlink();(root/'0001.json').symlink_to(root/'0002.json')
                if case=='missing':(root/'0001.json').unlink()
                if case=='extra':(root/'other').write_text('unexpected')
                if case=='empty':
                    for path in root.iterdir():path.unlink()
                if case=='invalid-json':(root/'0001.json').write_text('{')
                with self.assertRaises(ValueError):audit.inspect(root,manifest)

    def test_invalid_event_order_and_resource_shapes_fail(self):
        for case in ('double-intent','orphan-result','wrong-action','bad-id','unexpected-id','unknown-action',
                     'unknown-event','post-complete','complete-pending','bad-complete','bad-plan'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'network';manifest,events=self.fixture(root)
                if case=='double-intent':events.insert(2,events[1])
                if case=='orphan-result':events.pop(1)
                if case=='wrong-action':events[2]['action']='create-subnet'
                if case=='bad-id':events[2]['id']='subnet-12345678'
                if case=='unexpected-id':
                    events[1]['action']=events[2]['action']='modify-vpc-attribute'
                if case=='unknown-action':events[1]['action']='delete-vpc'
                if case=='unknown-event':events[1]['event']='unknown'
                if case=='post-complete':events.insert(1,{'event':'complete','resources':{},'runtime_verified':False})
                if case=='complete-pending':events[-1]={'event':'complete','resources':{},'runtime_verified':False}
                if case=='bad-complete':events.append({'event':'complete','resources':{},'runtime_verified':True})
                if case=='bad-plan':events[0]['plan']['deployment']='foreign'
                self.write(root,events)
                with self.assertRaises(ValueError):audit.inspect(root,manifest)

if __name__=='__main__':unittest.main()
