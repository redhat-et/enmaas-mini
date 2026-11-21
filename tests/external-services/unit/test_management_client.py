"""Synthetic profile bridge cannot patch roles, arbitrary URLs or real emails."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

spec=importlib.util.spec_from_file_location('fixture_management',(Path(__file__).resolve().parents[1] / 'management-client.py'))
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)

class ProfileBridge(unittest.TestCase):
    def command(self, tags, identity='11111111-1111-4111-8111-111111111111'):
        return {'method':'PATCH','route':'profile','body':{'user_id':identity,'tags':tags}}
    def invoke(self, command):
        with patch.object(client.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(command).encode()))),contextlib.redirect_stdout(io.StringIO()):
            client.main()
    def test_profile_scope_is_checked_before_secret_read(self):
        email='lab-run-'+'a'*32+'-0@example.test'
        for tags,identity in [({'email':email,'role':'super_admin'},'11111111-1111-4111-8111-111111111111'),
                              ({'email':'real@example.com'},'11111111-1111-4111-8111-111111111111'),
                              ({'email':'lab-run-'+'a'*32+'-2500@example.test'},'11111111-1111-4111-8111-111111111111'),
                              ({'email':email},'../../admin')]:
            with patch.object(client.Path,'read_text') as secret:
                with self.assertRaises(ValueError): self.invoke(self.command(tags,identity))
                secret.assert_not_called()
    def test_profile_uses_partner_api_and_leaves_role_unchanged(self):
        tags={'email':'lab-run-'+'a'*32+'-0@example.test','country':'US','first_name':'Maya','last_name':'Chen'}
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{"active":true}'
        opener=MagicMock();opener.open.return_value=response
        with patch.object(client.Path,'read_text',return_value='synthetic-key'),patch.object(client.urllib.request,'build_opener',return_value=opener):
            self.invoke(self.command(tags))
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:8087/api/v1/users/11111111-1111-4111-8111-111111111111')
        self.assertEqual(request.method,'PATCH')
        self.assertEqual(json.loads(request.data),{'tags':tags})

class PersistentBridge(unittest.TestCase):
    def test_commands_are_bounded_and_replies_flushed_without_logs(self):
        commands=[{'route':'users','method':'PUT','body':{'subject':'test'}},
                  {'route':'budgets','method':'PUT','body':{'monthly_usd':300}}]
        incoming=io.BytesIO(b''.join(json.dumps(row).encode()+b'\n' for row in commands))
        output=io.StringIO()
        with patch.object(client.sys,'stdin',SimpleNamespace(buffer=incoming)),contextlib.redirect_stdout(output),patch.object(client,'execute',return_value={'ok':True}) as execute:
            client.serve()
        self.assertEqual(execute.call_count,2)
        self.assertEqual(output.getvalue(),'{"ok": true}\n'*2)

    def test_oversized_or_unterminated_command_is_rejected(self):
        for data in (b'x'*65537,b'{}'):
            with patch.object(client.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(data))),patch.object(client,'execute') as execute:
                with self.assertRaises(ValueError):client.serve()
                execute.assert_not_called()
