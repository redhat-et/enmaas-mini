"""Real HTTP mock catalogs and protocol-specific model/credential boundaries."""
import importlib.util
import json
from pathlib import Path
import unittest
import urllib.error
import urllib.request

path = Path(__file__).resolve().parents[1] / 'common/provider.py'
spec = importlib.util.spec_from_file_location('profile_test_provider', path)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class ProfileBackend(unittest.TestCase):
    def test_catalogs_inference_unknown_model_and_wrong_key(self):
        fixture = module.Provider(ports=(0, 0, 0), models_by_api={
            'openai': ['gpt-5.4-mini'], 'anthropic': ['claude-sonnet-5-5']})
        fixture.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for index, model, route, headers in (
                (0, 'gpt-5.4-mini', '/v1/chat/completions', {'Authorization': 'Bearer synthetic-openai'}),
                (1, 'claude-sonnet-5-5', '/v1/messages', {'x-api-key': 'synthetic-anthropic'})):
                url = 'http://127.0.0.1:' + str(fixture.servers[index].server_address[1])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(url + '/v1/models', timeout=3)
                self.assertEqual(error.exception.code, 403)
                with opener.open(urllib.request.Request(url + '/v1/models', headers=headers), timeout=3) as response:
                    self.assertEqual([m['id'] for m in json.load(response)['data']], [model])
                def request(name, auth):
                    return urllib.request.Request(url + route, headers={'Content-Type': 'application/json', **auth},
                        data=json.dumps({'model': name, 'messages': [{'role': 'user', 'content': 'test'}],
                                         'max_tokens': 32}).encode())
                with opener.open(request(model, headers), timeout=3) as response:
                    self.assertEqual(json.load(response)['model'], model)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(request('unavailable', headers), timeout=3)
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(request(model, {}), timeout=3)
                self.assertEqual(error.exception.code, 403)
        finally:
            fixture.close()

if __name__ == '__main__':
    unittest.main()
