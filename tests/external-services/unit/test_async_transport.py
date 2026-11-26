import json
from pathlib import Path
import ssl
import tempfile
import unittest

try:
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from async_transport import AsyncTransport, response_duration
except ImportError:
    AsyncTransport = None

from test_transport import local
from transport import InvalidReply, UnexpectedResponse


@unittest.skipIf(AsyncTransport is None, 'Install stress-requirements.txt for async HTTP tests')
class AsyncTransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        local.issue_certificates(self.root)
        self.mode, self.calls = 'ok', []
        async def handler(request):
            body = await request.json()
            self.calls.append((dict(request.headers), body))
            if self.mode == 'redirect':
                raise web.HTTPFound('/other')
            if self.mode == 'large':
                return web.Response(body=b'x'*(1024*1024+1))
            if self.mode == 'failed':
                return web.Response(status=503, text='mock capacity')
            if self.mode == 'budget':
                return web.Response(status=429, text='budget denied')
            usage = {'prompt_tokens':128,'completion_tokens':32,'total_tokens':160}
            return web.Response(body=b'data: '+json.dumps({'usage':usage}).encode()+b'\n\ndata: [DONE]\n\n',
                                content_type='text/event-stream')
        app = web.Application()
        app.router.add_post('/{path:.*}', handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root/'server.pem', self.root/'server.key')
        self.server = TestServer(app, scheme='https')
        await self.server.start_server(ssl=context)
        self.url = str(self.server.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.server.close()
        self.temp.cleanup()

    def client(self, ca=None, concurrency=2):
        work = dict(input_tokens=128,output_tokens=32,ttft_ms=0,chunk_ms=0,task='synthetic probe')
        return AsyncTransport(self.url,ca or self.root/'ca.pem',['test-key'],'fixture',
            profiles=[{'model':'fixture','api':'openai','workload':work}],
            user_agents=['synthetic-test'],concurrency=concurrency)

    async def test_large_connector_limit_is_explicit_and_bounded(self):
        async with self.client(concurrency=40000) as client:
            self.assertEqual(client.session.connector.limit,40000)
            self.assertEqual(client.session.connector.limit_per_host,40000)
        for bound in (0,40001,True):
            with self.assertRaises(ValueError):self.client(concurrency=bound)

    async def test_verified_tls_usage_and_bounded_slow_instruction(self):
        async with self.client() as client:
            result = await client(0,150)
            self.assertEqual(result['status'],200)
            self.assertEqual(result['mock_duration_seconds'],10)
            result = await client(0,150)
            self.assertEqual(result['mock_duration_seconds'],48)
        headers, body = self.calls[0]
        self.assertEqual(headers['Authorization'],'Bearer test-key')
        self.assertFalse(any(k.lower().startswith('x-forwarded') for k in headers))
        instruction = json.loads(body['messages'][0]['content'].split('\n')[0].split(' ',1)[1])
        self.assertEqual(instruction['duration_ms'],10000)
        self.assertTrue(body['messages'][0]['content'].endswith('data'*128))

    async def test_redirect_body_bounds_and_budget_rejection(self):
        for mode, error in [('redirect',UnexpectedResponse),('large',InvalidReply),('budget',UnexpectedResponse)]:
            self.mode = mode
            before = len(self.calls)
            async with self.client() as client:
                with self.assertRaises(error):await client(0,150)
            self.assertEqual(len(self.calls),before+1)
        self.mode = 'failed'
        async with self.client() as client:
            self.assertEqual((await client(0,150))['status'],503)

    async def test_untrusted_ca_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory)
            local.issue_certificates(other)
            async with self.client(other/'ca.pem') as client:
                with self.assertRaises(ssl.SSLError):await client(0,150)

@unittest.skipIf(AsyncTransport is None,'Install stress-requirements.txt')
class DurationSchedule(unittest.TestCase):
    def test_every_supported_range_visits_all_durations(self):
        for maximum in range(10,121):
            values=[response_duration(i,10,maximum) for i in range(maximum-9)]
            self.assertEqual(set(values),set(range(10,maximum+1)))
            self.assertEqual(len(set(values)),len(values))
        self.assertEqual(response_duration(111,10,120),10)
