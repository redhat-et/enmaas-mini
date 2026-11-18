#!/usr/bin/env python3
"""External identity provisioning uses only the private partner API."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/pricetag'))
loader=importlib.machinery.SourceFileLoader('oidc_user',str(ROOT/'scripts/pricetag/oidc-user'))
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec);loader.exec_module(module)

class Identity(unittest.TestCase):
    def test_profile_requires_explicit_immutable_uuid_and_display_attributes(self):
        valid={'user_id':'9fc73c22-ff5a-4037-b8b1-78b2d96b2f38','tags':{
            'email':'alice@example.test','first_name':'Alice','last_name':'Lab'}}
        self.assertEqual(module.validate(valid),valid)
        for document in ({**valid,'user_id':'alice'}, {**valid,'role':'super_admin'},
                         {**valid,'tags':{'email':'alice@example.test'}},
                         {**valid,'tags':{**valid['tags'],'role':'super_admin'}}):
            with self.assertRaises(ValueError):module.validate(document)

    def test_api_uses_separate_secret_no_proxy_no_redirect_and_bounded_timeout(self):
        payload={'user_id':'9fc73c22-ff5a-4037-b8b1-78b2d96b2f38','tags':{
            'email':'alice@example.test','first_name':'Alice','last_name':'Lab'}}
        with tempfile.TemporaryDirectory() as d, patch.object(module.os,'geteuid',return_value=0):
            root=Path(d);(root/'partner-secret').write_text('synthetic-partner-secret')
            with patch.object(module,'DEPLOY',root),patch.object(module.urllib.request,'build_opener') as build:
                response=MagicMock();response.read.return_value=json.dumps({'user_id':payload['user_id']}).encode()
                build.return_value.open.return_value.__enter__.return_value=response
                module.provision(payload)
                req=build.return_value.open.call_args.args[0]
                self.assertEqual(req.full_url,'http://127.0.0.1:18087/api/v1/users/'+payload['user_id'])
                self.assertEqual(req.method,'PUT')
                self.assertEqual(json.loads(req.data),{'tags':payload['tags']})
                self.assertEqual(build.return_value.open.call_args.kwargs['timeout'],30)
                self.assertEqual(build.call_args.args[0].proxies,{})
                self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-partner-secret')

    def test_profile_permissions_symlinks_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'profile.json'
            path.write_text('{}');path.chmod(0o644)
            with self.assertRaises(ValueError):module.private_json(path)
            link=Path(d)/'link';link.symlink_to(path)
            with self.assertRaises(OSError):module.private_json(link)
            path.write_bytes(b'x'*65537);path.chmod(0o600)
            real_fstat=os.fstat
            def root_owned(fd):
                values=list(real_fstat(fd));values[4]=0
                return os.stat_result(values)
            with patch.object(module.os,'fstat',side_effect=root_owned):
                with self.assertRaisesRegex(ValueError,'64 KiB'):module.private_json(path)

    def test_deactivation_requires_confirmed_inactive_identity_and_key_count(self):
        identity='9fc73c22-ff5a-4037-b8b1-78b2d96b2f38'
        payload={'user_id':identity,'tags':{'email':'test@example.test','first_name':'Test','last_name':'User'}}
        for result in ({'user_id':identity,'active':False,'keys_revoked':2},
                       {'user_id':identity,'active':True,'keys_revoked':2},
                       {'user_id':identity,'active':False,'keys_revoked':True},
                       {'user_id':identity,'active':False,'keys_revoked':-1}):
            with patch.object(module,'partner_request',return_value=result) as request:
                if result['active'] is False and type(result['keys_revoked']) is int and result['keys_revoked']>=0:
                    self.assertEqual(module.deactivate(payload),result)
                else:
                    with self.assertRaises(ValueError):module.deactivate(payload)
                request.assert_called_once_with(payload,'DELETE')

    def test_key_readback_is_bound_to_profile_uuid_and_rejects_invalid_shapes(self):
        profile={'user_id':'9fc73c22-ff5a-4037-b8b1-78b2d96b2f38','tags':{'email':'test@example.test','first_name':'Test','last_name':'User'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(module.os,'geteuid',return_value=0):
            root=Path(tmp);(root/'partner-secret').write_text('synthetic-secret')
            for payload in ({'keys':[]},{'keys':None},[]):
                with patch.object(module,'DEPLOY',root),patch.object(module.urllib.request,'build_opener') as build:
                    response=MagicMock();response.read.return_value=json.dumps(payload).encode()
                    build.return_value.open.return_value.__enter__.return_value=response
                    if payload=={'keys':[]}:self.assertEqual(module.list_keys(profile),[])
                    else:
                        with self.assertRaises(ValueError):module.list_keys(profile)
                    request=build.return_value.open.call_args.args[0]
                    self.assertEqual(request.full_url,'http://127.0.0.1:18087/api/v1/users/'+profile['user_id']+'/keys')
                    self.assertEqual(request.method,'GET');self.assertIsNone(request.data)

    def test_nonroot_does_not_read_secret_or_call_api(self):
        with patch.object(module.os,'geteuid',return_value=1000), patch.object(module.urllib.request,'build_opener') as build:
            with self.assertRaises(ValueError):module.provision({})
            build.assert_not_called()

if __name__=='__main__':unittest.main()
