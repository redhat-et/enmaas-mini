#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest
import json
import os
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('rds_bootstrap',Path(__file__).with_name('rds-bootstrap.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Bootstrap(unittest.TestCase):
    def test_password_is_not_sent_in_role_sql(self):
        passwords={'maas':'a'*64,'metering':'b'*64}
        sql=module.role_sql(passwords)
        for password in passwords.values():self.assertNotIn(password,sql)
        self.assertEqual(sql.count('SCRAM-SHA-256$4096:'),2)
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',sql)
        self.assertIn('REVOKE ALL ON DATABASE metering FROM PUBLIC',sql)
        with self.assertRaises(ValueError):module.role_sql({'maas':"bad'",'metering':'b'*64})

    def test_secret_validation(self):
        self.assertEqual(module.credentials(b'{"username":"labadmin","password":"a:b\\\\c"}'),('labadmin','a:b\\c'))
        for raw in (b'{"username":"admin:bad","password":"secret"}',b'{"username":"admin","password":"a\\nb"}',b'x'*16385):
            with self.assertRaises(ValueError):module.credentials(raw)

    def run_bootstrap(self, directory, fail_at=None, isolation='0\nf'):
        root=Path(directory);ca=root/'input-ca.pem';ca.write_text('test CA')
        args=SimpleNamespace(host='db.example.eu-central-1.rds.amazonaws.com',ca_file=ca,output=root/'output')
        calls=[]
        replies=['0','', 'maas:maas:true','metering:metering:true',isolation]
        def execute(command, **kwargs):
            index=len(calls);calls.append(kwargs)
            self.assertNotIn('password', ' '.join(command))
            env=kwargs['env']
            self.assertNotIn('AWS_SECRET_ACCESS_KEY',env)
            self.assertNotIn('PGPASSWORD',env)
            self.assertEqual(env['PGSSLMODE'],'verify-full')
            passfile=Path(env['PGPASSFILE'])
            self.assertEqual(passfile.stat().st_mode & 0o777,0o600)
            self.assertTrue(passfile.read_text())
            if fail_at==index:raise subprocess.TimeoutExpired(command,60)
            return SimpleNamespace(returncode=0,stdout=replies[index])
        with patch.object(module.os,'geteuid',return_value=0), patch.object(module.shutil,'which',return_value='/usr/bin/psql'), patch.object(module.subprocess,'run',side_effect=execute), patch.dict(os.environ,{'PGPASSWORD':'inherited-secret','AWS_SECRET_ACCESS_KEY':'aws-secret'}), patch('builtins.print'):
            try:module.bootstrap(args,b'{"username":"labadmin","password":"admin-secret"}')
            finally:
                for call in calls:self.assertFalse(Path(call['env']['PGPASSFILE']).exists())
        return args.output,calls

    def test_complete_bootstrap_and_secret_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            output,calls=self.run_bootstrap(directory)
            self.assertEqual(len(calls),5)
            state=json.loads((output/'bootstrap.json').read_text())
            self.assertEqual(state['stage'],'verified');self.assertTrue(state['complete'])
            self.assertEqual(output.stat().st_mode & 0o777,0o700)
            for item in output.iterdir():self.assertEqual(item.stat().st_mode & 0o777,0o600)
            self.assertNotIn('admin-secret',(output/'bootstrap.json').read_text())
            for call in calls:self.assertNotIn('admin-secret',call['input'])

    def test_failures_retain_recovery_inputs_without_success(self):
        for index in range(5):
            with self.subTest(index=index),tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(subprocess.TimeoutExpired):self.run_bootstrap(directory,fail_at=index)
                output=Path(directory)/'output'
                self.assertFalse(json.loads((output/'bootstrap.json').read_text())['complete'])
                self.assertTrue((output/'maas.url').is_file())
                self.assertTrue((output/'metering.url').is_file())

    def test_cross_database_access_refuses_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'isolation'):self.run_bootstrap(directory,isolation='0\nt')
            state=json.loads((Path(directory)/'output/bootstrap.json').read_text())
            self.assertFalse(state['complete']);self.assertEqual(state['stage'],'verify-role-isolation')

    def test_scram_is_salted(self):
        self.assertNotEqual(module.scram('a'*64),module.scram('a'*64))
        self.assertEqual(module.scram('a'*64,b'x'*16),module.scram('a'*64,b'x'*16))

if __name__=='__main__':unittest.main()
