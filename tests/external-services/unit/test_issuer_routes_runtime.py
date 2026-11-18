"""Cleanup must continue after any single Podman removal fails or times out."""
import importlib.util
import contextlib
import io
import json
import http.client
import ssl
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('routes',(Path(__file__).resolve().parents[1] / 'issuer-routes-runtime.py'))
routes=importlib.util.module_from_spec(spec);spec.loader.exec_module(routes)

class Cleanup(unittest.TestCase):
    def test_discovery_probe_rejects_host_header_poisoned_metadata(self):
        origin='https://127.0.0.1:8443/realms/enmaas-lab'
        document={'issuer':origin,**{key:origin+'/protocol/openid-connect/'+key for key in
            ('authorization_endpoint','token_endpoint','jwks_uri','userinfo_endpoint','end_session_endpoint')}}
        for mode in ('valid','issuer','endpoint'):
            class Connection:
                def __init__(self,*args,**kwargs):pass
                def request(self,method,path,headers):self.path=path;self.headers=headers
                def close(self):pass
                def getresponse(self):
                    allowed=self.path.endswith('/.well-known/openid-configuration') and self.path=='/realms/enmaas-lab/.well-known/openid-configuration'
                    data=dict(document)
                    if self.headers and mode!='valid':
                        data['issuer' if mode=='issuer' else 'token_endpoint']='https://attacker.example/realms/enmaas-lab'
                    return SimpleNamespace(status=200 if allowed else 404,
                        read=lambda limit:json.dumps(data).encode() if allowed else b'Route not available')
            with patch.object(http.client,'HTTPSConnection',Connection),patch.object(ssl,'create_default_context'), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                if mode=='valid':
                    exec(routes.CLIENT,{})
                    self.assertIn('"forwarded_header_identity_checks": 3',out.getvalue())
                else:
                    with self.assertRaises(AssertionError):exec(routes.CLIENT,{})

    def test_removes_only_recorded_resources(self):
        call=Mock(return_value=SimpleNamespace(returncode=0))
        routes.cleanup(call,'owned-pod',True,['owned-cert','owned-key'])
        self.assertEqual([c.args for c in call.call_args_list],[
            ('pod','rm','--force','--ignore','owned-pod'),
            ('secret','rm','--ignore','owned-cert'),('secret','rm','--ignore','owned-key')])
        self.assertTrue(all(c.kwargs=={'check':False,'timeout':10} for c in call.call_args_list))

    def test_failure_does_not_skip_remaining_secret_cleanup(self):
        for error in (OSError('SECRET'),subprocess.TimeoutExpired('podman',10),SimpleNamespace(returncode=1)):
            call=Mock(side_effect=[error,SimpleNamespace(returncode=0),SimpleNamespace(returncode=0)])
            with self.subTest(error=type(error).__name__),self.assertRaisesRegex(RuntimeError,'owned-pod') as failure:
                routes.cleanup(call,'owned-pod',True,['owned-cert','owned-key'])
            self.assertEqual(call.call_count,3)
            self.assertNotIn('SECRET',str(failure.exception))

    def test_partial_creation_and_secret_failure(self):
        call=Mock(side_effect=[OSError('secret failure'),SimpleNamespace(returncode=0)])
        with self.assertRaisesRegex(RuntimeError,'owned-cert'):
            routes.cleanup(call,'not-created',False,['owned-cert','owned-key'])
        self.assertEqual(call.call_count,2)
        call=Mock();routes.cleanup(call,'not-created',False,[]);call.assert_not_called()

if __name__=='__main__':unittest.main()
