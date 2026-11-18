"""Explicit selected-user retrieval must not expose issuer service credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('keycloak_password',(Path(__file__).resolve().parents[1] / 'keycloak-password.py'))
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
UID='8beb9bf1-7cb8-47fa-ac79-a72947000376'


class Password(unittest.TestCase):
    def test_only_inventory_users_can_be_selected(self):
        root=Path('/unused')
        with patch.object(helper.os,'geteuid',return_value=1),self.assertRaises(ValueError):helper.password(root,'admin')
        with patch.object(helper.os,'geteuid',return_value=0):
            with patch.object(helper,'read',side_effect=[{'admin':UID},{'admin':'user-password','db':'service-password'}]):
                self.assertEqual(helper.password(root,'admin'),'user-password')
            for user in ('db','oidc','gateway_oidc','bootstrap_admin','missing'):
                with patch.object(helper,'read',return_value={'admin':UID}) as read,self.assertRaises(ValueError):
                    helper.password(root,user)
                read.assert_called_once()
            for identity in (None,'bad-uuid',UID.upper()):
                with patch.object(helper,'read',return_value={'admin':identity}),self.assertRaises(ValueError):helper.password(root,'admin')
            for value in (None,'','x'*1025,'line\nbreak','\x7f'):
                with patch.object(helper,'read',side_effect=[{'admin':UID},{'admin':value}]),self.assertRaises(ValueError):helper.password(root,'admin')

    def test_population_password_requires_completed_exact_identity(self):
        root=Path('/unused');prefix='perf-'+'a'*16
        manifest={'version':1,'prefix':prefix,'count':2}
        record={'username':prefix+'-00002','password':'s'*43,'state':'created','user_id':UID}
        with patch.object(helper.os,'geteuid',return_value=1),self.assertRaises(ValueError):
            helper.population_password(root,2)
        with patch.object(helper.os,'geteuid',return_value=0):
            with patch.object(helper,'read',side_effect=[manifest,record]) as read:
                self.assertEqual(helper.population_password(root,2),'s'*43)
                self.assertEqual(read.call_args.args[0],root/'00002.json')
            for index in (0,3,-1,True,'../credentials'):
                with patch.object(helper,'read',return_value=manifest) as read,self.assertRaises(ValueError):
                    helper.population_password(root,index)
                read.assert_called_once()
            for change in ({'state':'creating'},{'username':'admin'},{'user_id':'bad'},
                           {'password':'short'},{'password':'s'*42+'\n'},{'extra':'secret'}):
                with patch.object(helper,'read',side_effect=[manifest,dict(record,**change)]),self.assertRaises(ValueError):
                    helper.population_password(root,2)
            for change in ({'version':2},{'prefix':'foreign'},{'count':10001},{'count':True},{'extra':'bad'}):
                with patch.object(helper,'read',return_value=dict(manifest,**change)),self.assertRaises(ValueError):
                    helper.population_password(root,2)

    def test_population_cli_does_not_accept_bootstrap_inputs(self):
        args=[str(Path(helper.__file__)),'--index','2','--population','/root/population','--stdout']
        with patch('sys.argv',args),patch.object(helper,'population_password',return_value='selected-only') as get,contextlib.redirect_stdout(io.StringIO()) as out:
            helper.main()
        get.assert_called_once_with(Path('/root/population'),2)
        self.assertEqual(out.getvalue(),'selected-only')
        for invalid in (['--index','2','--stdout'],['--user','admin','--population','/root/population','--stdout'],
                        ['--index','2','--population','/root/population','--inputs','/root/inputs','--stdout']):
            with patch('sys.argv',[args[0],*invalid]),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):helper.main()

    def test_private_file_contract_and_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data';p.write_text('{}');p.chmod(0o600)
            good=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=0)
            directory=SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=0)
            with patch.object(helper.os,'fstat',side_effect=lambda fd: directory if stat.S_ISDIR(os.stat('/dev/fd/'+str(fd)).st_mode) else good):
                self.assertEqual(helper.read(p),{})
                p.write_text('[]')
                with self.assertRaises(ValueError):helper.read(p)
                p.write_bytes(b'x'*(8*1024*1024+1))
                with self.assertRaisesRegex(ValueError,'size limit'):helper.read(p)
            for mode,uid in ((stat.S_IFREG|0o644,0),(stat.S_IFREG|0o600,1000),(stat.S_IFDIR|0o700,0)):
                with patch.object(helper.os,'fstat',side_effect=[directory,SimpleNamespace(st_mode=mode,st_uid=uid)]),self.assertRaises(ValueError):helper.read(p)
            for mode,uid in ((stat.S_IFDIR|0o755,0),(stat.S_IFDIR|0o700,1000)):
                with patch.object(helper.os,'fstat',return_value=SimpleNamespace(st_mode=mode,st_uid=uid)),self.assertRaises(ValueError):helper.read(p)
            link=Path(tmp)/'link';link.symlink_to(p)
            with patch.object(helper.os,'fstat',return_value=directory),self.assertRaises(OSError):helper.read(link)
            alias=Path(tmp)/'alias';alias.symlink_to(Path(tmp),target_is_directory=True)
            with self.assertRaises(OSError):helper.read(alias/'data')

    def test_cli_requires_explicit_output_and_prints_only_selected_password(self):
        args=[str(Path(helper.__file__)),'--user','admin']
        with patch('sys.argv',args),patch.object(helper,'password') as get,self.assertRaises(ValueError):helper.main()
        get.assert_not_called()
        with patch('sys.argv',args+['--stdout']),patch.object(helper,'password',return_value='selected-only'),contextlib.redirect_stdout(io.StringIO()) as out:
            helper.main()
        self.assertEqual(out.getvalue(),'selected-only')
        result=subprocess.run([sys.executable,*args],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,'')
        self.assertIn('retrieval failed',result.stderr)


if __name__=='__main__':unittest.main()
