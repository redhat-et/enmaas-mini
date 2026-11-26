"""Real HTTP mock catalogs and protocol-specific model/credential boundaries."""
import importlib.util
import json
import time
from pathlib import Path
import unittest
import urllib.error
import urllib.request

path = Path(__file__).resolve().parents[2] / 'common/provider.py'
spec = importlib.util.spec_from_file_location('profile_test_provider', path)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class ProfileBackend(unittest.TestCase):
    def test_workload_bounds_and_streamed_usage_follow_simulated_counts(self):
        body={'messages':[{'content':'ENMAAS_MOCK_WORKLOAD '+json.dumps({
            'input_tokens':2048,'output_tokens':384,'ttft_ms':100,'chunk_ms':50})+'\nReview a patch.'}]}
        self.assertEqual(module.synthetic_workload(body)['input_tokens'],2048)
        self.assertIsNone(module.synthetic_workload({'messages':[{'content':'ordinary request'}]}))
        for value in (-1,True,999999):
            bad={'messages':[{'content':'ENMAAS_MOCK_WORKLOAD '+json.dumps({
                'input_tokens':value,'output_tokens':384,'ttft_ms':100,'chunk_ms':50})}]}
            with self.assertRaises(ValueError):module.synthetic_workload(bad)
        result=module.completion('/v1/messages',{'model':'fixture'})
        result['usage']={'input_tokens':2048,'output_tokens':384}
        stream=b''.join(module.events('/v1/messages',result))
        self.assertIn(b'"input_tokens": 2048',stream)
        self.assertIn(b'"output_tokens": 384',stream)

    def test_simulated_usage_is_opt_in_and_has_bounded_latency(self):
        instruction={'input_tokens':2048,'output_tokens':384,'ttft_ms':30,'chunk_ms':0}
        body={'model':'fixture','messages':[{'role':'user','content':'ENMAAS_MOCK_WORKLOAD '+json.dumps(instruction)}]}
        for enabled in (False,True):
            fixture=module.Provider(ports=(0,0,0),synthetic_workloads=enabled)
            fixture.start()
            try:
                url='http://127.0.0.1:'+str(fixture.servers[0].server_address[1])+'/v1/chat/completions'
                request=urllib.request.Request(url,data=json.dumps(body).encode(),headers={
                    'Authorization':'Bearer synthetic-openai','Content-Type':'application/json'})
                started=time.monotonic()
                with urllib.request.urlopen(request,timeout=3) as reply:result=json.load(reply)
                self.assertEqual(result['usage']['prompt_tokens'],2048 if enabled else 2)
                if enabled:self.assertGreaterEqual(time.monotonic()-started,.025)
            finally:fixture.close()

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
