"""Public-zone selection never trusts display text as shell code or changes AWS."""
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('zone_select',(Path(__file__).resolve().parents[1] / 'zone-select.py'))
z=importlib.util.module_from_spec(spec);spec.loader.exec_module(z)


class Selection(unittest.TestCase):
    def document(self):
        return {'HostedZones':[
            {'Id':'/hostedzone/Z123','Name':'example.test.','Config':{'PrivateZone':False}},
            {'Id':'/hostedzone/Z456','Name':'private.test.','Config':{'PrivateZone':True}}]}

    def test_public_only_and_safe_exports(self):
        rows=z.catalog(self.document())
        self.assertEqual(rows,[('Z123','example.test')])
        terminal=io.StringIO()
        self.assertEqual(z.select(rows,lambda:'1',terminal),('Z123','example.test'))
        self.assertNotIn('private.test',terminal.getvalue())
        for answer in ('','0'):
            self.assertEqual(z.select(rows,lambda:answer,io.StringIO()),('',''))
        self.assertEqual(z.select([],lambda:self.fail('unexpected prompt'),io.StringIO()),('',''))
        for answer in ('2','-1','$(id)','1; echo x'):
            with self.assertRaises(ValueError):z.select(rows,lambda:answer,io.StringIO())

    def test_rejects_partial_duplicate_malformed_or_unsafe_metadata(self):
        for value in ({}, {'HostedZones':None}, {**self.document(),'NextToken':'more'},
                      {**self.document(),'IsTruncated':True}):
            with self.assertRaises(ValueError):z.catalog(value)
        for field,value in (('Id','Z1;id'),('Name','$(id).test.'),('Name','EXAMPLE.test.'),
                            ('Name','-bad.test.'),('Name','onlylabel'),('Config',{}),
                            ('Config',{'PrivateZone':'false'})):
            doc=self.document();doc['HostedZones'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):z.catalog(doc)
        doc=self.document();doc['HostedZones'].append(doc['HostedZones'][0])
        with self.assertRaises(ValueError):z.catalog(doc)

    def test_cli_rejects_invalid_input_without_echoing_it(self):
        result=subprocess.run([sys.executable,str(Path(z.__file__))],input='SECRET_INVALID_JSON',
                              capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Zone selection stopped',result.stderr)
        self.assertNotIn('SECRET',result.stdout+result.stderr)

    def test_main_uses_tty_for_choice_and_stdout_only_for_exports(self):
        terminal=io.StringIO('1\n');output=io.StringIO()
        with patch.object(z.sys,'stdin',io.StringIO(json.dumps(self.document()))), \
             patch.object(z.sys,'stdout',output),patch('builtins.open') as opened, \
             patch.object(z,'select',return_value=('Z123','example.test')):
            opened.return_value.__enter__.return_value=terminal
            z.main()
        self.assertEqual([call.args for call in opened.call_args_list],[('/dev/tty','r'),('/dev/tty','w')])
        self.assertEqual(output.getvalue(),'export ZONE_ID=Z123\nexport DNS_DOMAIN=example.test\n')


if __name__=='__main__':unittest.main()
