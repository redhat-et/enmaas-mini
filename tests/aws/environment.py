"""Selected-profile isolation and public AWS endpoints without reading credentials."""
import importlib.util
from pathlib import Path
import os
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('test_aws_environment',ROOT/'scripts/aws/environment.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Environment(unittest.TestCase):
    def test_profile_discards_other_principal_and_endpoint_overrides(self):
        inherited={'AWS_ACCESS_KEY_ID':'synthetic-admin','AWS_SECRET_ACCESS_KEY':'not-valid',
            'AWS_SESSION_TOKEN':'not-valid','AWS_SECURITY_TOKEN':'not-valid','AWS_PROFILE':'admin',
            'AWS_DEFAULT_PROFILE':'admin','AWS_ROLE_ARN':'arn:example','AWS_WEB_IDENTITY_TOKEN_FILE':'/tmp/other',
            'AWS_ENDPOINT_URL':'http://untrusted.invalid','AWS_ENDPOINT_URL_EC2':'http://untrusted.invalid',
            'AWS_IGNORE_CONFIGURED_ENDPOINT_URLS':'false','AWS_MAX_ATTEMPTS':'10','AWS_CLI_AUTO_PROMPT':'on',
            'AWS_CONFIG_FILE':'/selected/config','PATH':'/tools','HOME':'/home/operator'}
        with patch.dict(os.environ,inherited,clear=True):
            result=module.environment('operator')
            self.assertEqual(dict(os.environ),inherited)
        for name in inherited:
            if name in ('AWS_CONFIG_FILE','PATH','HOME'):self.assertEqual(result[name],inherited[name])
            elif name not in ('AWS_IGNORE_CONFIGURED_ENDPOINT_URLS','AWS_MAX_ATTEMPTS','AWS_CLI_AUTO_PROMPT'):
                self.assertNotIn(name,result)
        self.assertEqual(result['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
        self.assertEqual(result['AWS_MAX_ATTEMPTS'],'1');self.assertEqual(result['AWS_CLI_AUTO_PROMPT'],'off')

    def test_environment_only_credentials_preserved_without_profile(self):
        with patch.dict(os.environ,{'AWS_ACCESS_KEY_ID':'synthetic','AWS_SECRET_ACCESS_KEY':'not-valid',
                 'AWS_SESSION_TOKEN':'not-valid','AWS_ENDPOINT_URL_STS':'http://untrusted.invalid'},clear=True):
            result=module.environment()
        self.assertEqual(result['AWS_ACCESS_KEY_ID'],'synthetic')
        self.assertEqual(result['AWS_SESSION_TOKEN'],'not-valid')
        self.assertNotIn('AWS_ENDPOINT_URL_STS',result)
        self.assertEqual(result['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')

if __name__=='__main__':unittest.main()
