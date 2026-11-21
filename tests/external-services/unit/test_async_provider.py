import json
from pathlib import Path
import time
import unittest

try:
    from aiohttp.test_utils import TestClient, TestServer
    import async_provider
except ImportError:
    async_provider = None


@unittest.skipIf(async_provider is None, 'Install stress-requirements.txt for async HTTP tests')
class AsyncProvider(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.common = async_provider.load_common(Path(__file__).resolve().parents[2]/'common/provider.py')
        self.work = {'input_tokens':128,'output_tokens':32,'ttft_ms':0,'chunk_ms':0,'duration_ms':10000}
        self.body = {'model':'fixture','stream':True,'messages':[{'role':'user',
                     'content':'ENMAAS_MOCK_WORKLOAD '+json.dumps(self.work)}]}

    async def test_auth_model_validation_and_capacity_rejection(self):
        capacity = async_provider.Capacity(1)
        app = async_provider.application(self.common, ['fixture'], 'test-key', 'openai', capacity)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/v1/chat/completions', json=self.body)
            self.assertEqual(response.status, 401)
            headers = {'Authorization':'Bearer test-key'}
            response = await client.post('/v1/chat/completions', json=dict(self.body, model='unknown'), headers=headers)
            self.assertEqual(response.status, 404)
            capacity.active = 1
            response = await client.post('/v1/chat/completions', json=self.body, headers=headers)
            self.assertEqual(response.status, 503)
            self.assertEqual(capacity.rejected, 1)
            self.assertEqual(capacity.active, 1)
            response=await client.get('/__enmaas/capacity')
            self.assertEqual(response.status,401)
            response=await client.get('/__enmaas/capacity',headers=headers)
            self.assertEqual(await response.json(),{'maximum':1,'active':1,'peak':0,'rejected':1})

    async def test_real_slow_stream_has_final_usage_and_releases_capacity(self):
        from transport import MockTransport
        for api, path, headers in [('openai','/v1/chat/completions',{'Authorization':'Bearer test-key'}),
                                   ('anthropic','/v1/messages',{'x-api-key':'test-key'})]:
            capacity = async_provider.Capacity(2)
            app = async_provider.application(self.common, ['fixture'], 'test-key', api, capacity)
            async with TestClient(TestServer(app)) as client:
                started = time.monotonic()
                response = await client.post(path, json=self.body, headers=headers)
                self.assertEqual(response.status, 200)
                first_frame = await response.content.readuntil(b'\n\n')
                self.assertLess(time.monotonic()-started,2)
                second_frame = await response.content.readuntil(b'\n\n')
                self.assertGreater(time.monotonic()-started,.2)
                data = first_frame+second_frame+await response.read()
                self.assertGreater(data.count(b'\n\n'),8)
                self.assertGreaterEqual(time.monotonic()-started, 9.9)
                validator = object.__new__(MockTransport)
                validator.stream = True
                validator.validate(data, api, (128,32))
                self.assertEqual(capacity.active, 0)
                self.assertEqual(capacity.peak,1)

    def test_explicit_large_capacity_remains_bounded(self):
        self.assertEqual(async_provider.Capacity().maximum,40000)
        self.assertEqual(async_provider.Capacity(40000).active,0)
        for maximum in (0,40001,True):
            with self.assertRaises(ValueError):async_provider.Capacity(maximum)

    def test_duration_and_payload_bounds(self):
        self.assertEqual(async_provider.instruction(self.body,self.common)[1],10)
        for duration in (0,9999,120001,True,'10000'):
            body = dict(self.body,messages=[{'content':'ENMAAS_MOCK_WORKLOAD '+json.dumps(dict(self.work,duration_ms=duration))}])
            with self.assertRaises(ValueError):async_provider.instruction(body,self.common)
