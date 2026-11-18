"""Private, bounded asynchronous model fixture; never a real provider adapter."""
import asyncio
import importlib.util
import json
from pathlib import Path
import time

from aiohttp import web
from stream_payload import streamed_frames


def load_common(path):
    spec = importlib.util.spec_from_file_location('fixture_provider', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instruction(body, common):
    messages = body.get('messages', [])
    content = messages[0].get('content') if messages and isinstance(messages[0], dict) else None
    prefix = 'ENMAAS_MOCK_WORKLOAD '
    if not isinstance(content, str) or not content.startswith(prefix):
        raise ValueError('Explicit mock workload required')
    raw = content[len(prefix):].split('\n', 1)[0]
    if len(raw) > 512:
        raise ValueError('Workload instruction too large')
    values = json.loads(raw)
    duration = values.pop('duration_ms')
    if type(duration) is not int or not 10000 <= duration <= 120000:
        raise ValueError('Mock duration must be 10–120 seconds')
    cleaned = dict(body, messages=[{'role':'user', 'content':prefix+json.dumps(values)}])
    work = common.synthetic_workload(cleaned)
    return work, duration/1000


class Capacity:
    def __init__(self, maximum=40000):
        if type(maximum) is not int or not 1 <= maximum <= 40000:
            raise ValueError('Invalid mock concurrency bound')
        self.maximum, self.active, self.rejected, self.peak = maximum, 0, 0, 0


def application(common, models, key, api, capacity):
    async def handle(request):
        # Fixed fixture keys; no redirect or credential forwarding path exists.
        authorized = (request.headers.get('Authorization') == 'Bearer '+key or
                      (api == 'anthropic' and request.headers.get('x-api-key') == key))
        if not authorized:
            return web.json_response({'error':'unauthorized'}, status=401)
        if request.method == 'GET' and request.path == '/__enmaas/capacity':
            return web.json_response({'maximum':capacity.maximum,'active':capacity.active,
                                      'peak':capacity.peak,'rejected':capacity.rejected})
        if request.method == 'GET' and request.path == '/v1/models':
            return web.json_response({'object':'list', 'data':[{'id':m,'object':'model'} for m in models]})
        allowed = ('/v1/messages',) if api == 'anthropic' else ('/v1/chat/completions','/v1/responses')
        if request.method != 'POST' or request.path not in allowed:
            return web.json_response({'error':'unsupported path'}, status=404)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('model') not in models:
                return web.json_response({'error':'unknown model'}, status=404)
            if body.get('stream') is not True:
                return web.json_response({'error':'streaming required'}, status=400)
            work, duration = instruction(body, common)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return web.json_response({'error':'invalid bounded mock workload'}, status=400)
        if capacity.active >= capacity.maximum:
            capacity.rejected += 1
            return web.json_response({'error':'mock concurrency ceiling'}, status=503)
        capacity.active += 1
        capacity.peak = max(capacity.peak,capacity.active)
        try:
            result = common.completion(request.path, body, False, declared_tools_only=True)
            incoming, outgoing = work['input_tokens'], work['output_tokens']
            if request.path == '/v1/chat/completions':
                result['usage'].update(prompt_tokens=incoming, completion_tokens=outgoing,
                                       total_tokens=incoming+outgoing)
            else:
                result['usage'].update(input_tokens=incoming, output_tokens=outgoing)
                if 'total_tokens' in result['usage']:
                    result['usage']['total_tokens'] = incoming+outgoing
            frame_count, frames = streamed_frames(common, request.path, result, outgoing)
            started = time.monotonic()
            await asyncio.sleep(work['ttft_ms']/1000)
            response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            for index, frame in enumerate(frames):
                # Absolute deadlines avoid accumulating per-chunk timing drift.
                if index:
                    target = started+work['ttft_ms']/1000+(duration-work['ttft_ms']/1000)*index/(frame_count-1)
                    await asyncio.sleep(max(0, target-time.monotonic()))
                await response.write(frame)
            await response.write_eof()
            return response
        except (ConnectionError, asyncio.CancelledError):
            raise
        finally:
            capacity.active -= 1
    app = web.Application(client_max_size=256*1024)
    app.router.add_route('*', '/{path:.*}', handle)
    return app


async def main():
    common = load_common('/fixtures/provider.py')
    document = json.loads(Path('/fixtures/mock-profiles.json').read_text())
    capacity = Capacity()
    runners = []
    try:
        for provider, ports, key in (('openai',(18080,18081),'synthetic-openai'),
                                     ('external',(18180,18181),'synthetic-anthropic')):
            for api, port in zip(('openai','anthropic'), ports):
                models = [m['model'] for m in document['models'] if m['provider']==provider and api in m['apis']]
                runner = web.AppRunner(application(common, models, key, api, capacity),
                                       access_log=None, shutdown_timeout=5)
                await runner.setup()
                await web.TCPSite(runner, '127.0.0.1', port, backlog=1024).start()
                runners.append(runner)
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
