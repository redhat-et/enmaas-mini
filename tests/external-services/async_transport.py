"""Verified HTTPS stress transport with bounded bodies and no credential redirects."""
import json
import math
import time

import aiohttp

from transport import MAX_BODY, InvalidReply, MockTransport, UnexpectedResponse


def response_duration(sequence,minimum,maximum):
    """A complete deterministic permutation of every integer in the range."""
    if type(sequence) is not int or sequence<0 or not 10<=minimum<=maximum<=120:
        raise ValueError('Invalid duration schedule')
    width=maximum-minimum+1
    stride=37
    while math.gcd(stride,width)!=1:stride+=1
    return minimum+(sequence*stride % width)


class AsyncTransport:
    def __init__(self, url, ca, keys, model, *, profiles, user_agents,
                 concurrency, minimum_seconds=10, maximum_seconds=120):
        if (type(concurrency) is not int or not 1 <= concurrency <= 40000 or
                any(type(v) is not int for v in (minimum_seconds,maximum_seconds)) or
                not 10 <= minimum_seconds <= maximum_seconds <= 120):
            raise ValueError('Invalid stress transport bounds')
        self.definition = MockTransport(url, ca, keys, model, profiles=profiles,
                                        user_agents=user_agents, watchdog=False)
        self.url, self.concurrency = url.rstrip('/'), concurrency
        self.minimum, self.maximum = minimum_seconds, maximum_seconds
        self.sequence = 0
        self.session = None

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(ssl=self.definition.context,
            limit=self.concurrency, limit_per_host=self.concurrency)
        self.session = aiohttp.ClientSession(connector=connector, trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            headers={'Accept-Encoding':'identity'})
        return self

    async def __aexit__(self, *args):
        await self.session.close()
        self.definition.close()

    async def __call__(self, user, timeout):
        if type(user) is not int or not 0 <= user < len(self.definition.keys):
            raise ValueError('Invalid stress user')
        if type(timeout) not in (int,float) or not self.maximum < timeout <= 180:
            raise ValueError('Deadline must exceed maximum response duration, up to 180 seconds')
        profile = self.definition.profiles[user]
        work = profile['workload']
        body = json.loads(self.definition.bodies[user])
        duration = response_duration(self.sequence,self.minimum,self.maximum)
        self.sequence += 1
        instruction = {k:work[k] for k in ('input_tokens','output_tokens','ttft_ms','chunk_ms')}
        instruction['duration_ms'] = duration*1000
        body['messages'] = [{'role':'user','content':'ENMAAS_MOCK_WORKLOAD '+json.dumps(instruction)+'\n'+work['task']+'\n'+('data'*work['input_tokens'])}]
        api = profile['api']
        headers = {'User-Agent':self.definition.user_agents[user]}
        if api == 'anthropic':
            headers.update({'x-api-key':self.definition.keys[user],'anthropic-version':'2023-06-01'})
        else:
            headers['Authorization'] = 'Bearer '+self.definition.keys[user]
        path = '/v1/messages' if api=='anthropic' else '/v1/chat/completions'
        started, first, size, chunks = time.monotonic(), None, 0, []
        async with self.session.post(self.url+path, json=body, headers=headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if 300 <= response.status < 400:
                raise UnexpectedResponse('Redirect refused')
            async for chunk in response.content.iter_chunked(65536):
                if first is None:first = time.monotonic()-started
                size += len(chunk)
                if size > MAX_BODY:raise InvalidReply('Response size bound exceeded')
                chunks.append(chunk)
            data = b''.join(chunks)
            if response.status == 200:
                self.definition.validate(data, api, (work['input_tokens'],work['output_tokens']))
            elif response.status == 429 and b'budget' in data.lower():
                raise UnexpectedResponse('Budget denial invalidates capacity comparison')
            return {'status':response.status,'bytes':size,'first_byte_seconds':first,
                    'mock_duration_seconds':duration,
                    'request_bytes':len(json.dumps(body).encode()),
                    'stream_events':data.count(b'\n\n') if response.status==200 else 0}
