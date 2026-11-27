"""Nonsecret operator profile creation preserves existing config and refuses unsafe writes."""
import configparser
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('operator_profile',(Path(__file__).resolve().parents[1] / 'operator-profile.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class Profile(unittest.TestCase):
    def settings(self,**kw):
        values=dict(account='123456789012',region='eu-central-1',group='demo',source='existing-user',mfa='arn:aws:iam::123456789012:mfa/operator')
        values.update(kw);return p.settings(**values)
    def test_settings_are_exact_nonsecret_and_require_mfa_for_user(self):
        name,values=self.settings();self.assertEqual(name,'enmaas-demo')
        self.assertEqual(values['role_arn'],'arn:aws:iam::123456789012:role/enmaas/em-demo-operator')
        self.assertEqual(values['duration_seconds'],'3600');self.assertEqual(values['source_profile'],'existing-user')
        self.assertEqual(set(values),{'role_arn','source_profile','region','duration_seconds','role_session_name','output','mfa_serial'})
        self.assertNotIn('mfa_serial',self.settings(mfa=None,role_source=True)[1])
        self.assertEqual(self.settings(mfa='GAHT12345678',profile='custom')[0],'custom')
        for change in ({'account':'wrong'},{'region':'bad'},{'group':'../x'},{'source':'name\nkey=value'},
                       {'profile':'default'},{'profile':'existing-user'},{'mfa':None},{'mfa':'arn:aws:iam::999999999999:mfa/other'},
                       {'role_source':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.settings(**change)
    def test_preserves_existing_bytes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config';before=b'# keep comment\n[profile existing-user]\nregion = eu-central-1\n'
            path.write_bytes(before);path.chmod(0o600)
            name,values=self.settings();result=p.append_profile(path,name,values)
            self.assertTrue(path.read_bytes().startswith(before));self.assertFalse(result['credentials_written'])
            self.assertFalse(result['role_assumed']);self.assertEqual(path.stat().st_mode&0o777,0o600)
            parser=configparser.RawConfigParser();parser.read(path)
            self.assertEqual(dict(parser['profile '+name]),values)
            after=path.read_bytes()
            with self.assertRaises(ValueError):p.append_profile(path,name,values)
            self.assertEqual(path.read_bytes(),after)
            self.assertEqual({x.name for x in path.parent.iterdir()},{'config','.enmaas-profile.lock'})
    def test_new_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config';p.append_profile(path,*self.settings())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_unsafe_paths_permissions_malformed_and_oversize_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=root/'public';parent.mkdir(mode=0o755)
            with self.assertRaises(ValueError):p.append_profile(parent/'config',*self.settings())
            path=root/'config';path.write_text('data');path.chmod(0o644)
            with self.assertRaises(ValueError):p.append_profile(path,*self.settings())
            path.chmod(0o600)
            with self.assertRaises(configparser.Error):p.append_profile(path,*self.settings())
            path.write_bytes(b'x'*(1024*1024+1))
            with self.assertRaises(ValueError):p.append_profile(path,*self.settings())
            path.unlink();path.symlink_to(root/'elsewhere')
            with self.assertRaises(OSError):p.append_profile(path,*self.settings())
            path.unlink();path.mkdir()
            with self.assertRaises((ValueError,OSError)):p.append_profile(path,*self.settings())
    def test_concurrent_config_change_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config'
            with patch.object(p,'read_config',side_effect=[b'',b'# concurrent update\n']):
                with self.assertRaisesRegex(ValueError,'changed'):p.append_profile(path,*self.settings())
            self.assertFalse(path.exists())
            self.assertFalse(any(x.name.startswith('.enmaas-profile-') for x in path.parent.iterdir()))
    def test_cli_preview_apply_and_redacted_failure(self):
        args=[p.__file__,'--account','123456789012','--group','demo','--source-profile','existing-user','--mfa-serial','GAHT12345678']
        with patch('sys.argv',args),contextlib.redirect_stdout(io.StringIO()) as output:p.main()
        self.assertFalse(json.loads(output.getvalue())['changed'])
        for options in (['--apply'],['--apply','--config-file','relative']):
            with patch('sys.argv',args+options),self.assertRaises(ValueError):p.main()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config'
            with patch('sys.argv',args+['--apply','--config-file',str(path)]),contextlib.redirect_stdout(io.StringIO()) as output:p.main()
            self.assertFalse(json.loads(output.getvalue())['credentials_written'])
        result=subprocess.run([sys.executable,*args,'--apply'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,'')
        self.assertIn('credential values are not printed',result.stderr)

if __name__=='__main__':unittest.main()
