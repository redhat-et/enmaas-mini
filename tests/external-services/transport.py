#!/usr/bin/env python3
"""Verified HTTPS transport for mock benchmark requests, not a standalone driver."""
import http.client
import ipaddress
import json
import math
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

MAX_BODY = 1024*1024

class InvalidReply(ValueError): pass
class UnexpectedResponse(ValueError): pass
class RequestTimeout(TimeoutError): pass


class MockTransport:
    """One watchdog per phase bounds slow headers/bodies without per-request threads.

The caller MUST validate the deployment is mock-only before constructing this
transport. Origins are limited to literal IP addresses or localhost so DNS cannot
introduce an unbounded lookup or redirect credentials to a different destination.
No proxy environment variables are consumed. Each request uses a new connection;
report that connection policy when interpreting throughput.
"""
    def __init__(self, url, ca, keys, model, stream=True, user_agents=None, profiles=None, watchdog=True, allow_budget_denials=False):
        origin = urlsplit(url)
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path not in ('', '/') or origin.query or origin.fragment):
            raise ValueError('require an HTTPS origin without credentials or path')
        self.allow_budget_denials = allow_budget_denials
        self.hostname = origin.hostname
        self.address = ipaddress.ip_address('127.0.0.1' if origin.hostname == 'localhost' else origin.hostname)
        self.port = origin.port or 443
        if not keys or any(not isinstance(key, str) or not key or len(key)>16384 or '\r' in key or '\n' in key for key in keys):
            raise ValueError('invalid benchmark credentials')
        if not isinstance(model,str) or not model or len(model)>256:
            raise ValueError('invalid mock model')
        if user_agents is None:
            user_agents = ['enmaas-synthetic-benchmark/1.0'] * len(keys)
        if len(user_agents) != len(keys) or any(not isinstance(value, str) or
                not 1 <= len(value) <= 256 or any(ord(c) < 32 or ord(c) > 126 for c in value)
                for value in user_agents):
            raise ValueError('invalid synthetic client metadata')
        self.user_agents = tuple(user_agents)
        self.keys = tuple(keys)
        self.context = ssl.create_default_context(cafile=str(ca))
        self.context.set_alpn_protocols(['http/1.1'])
        self.stream = stream
        payload = {'model':model, 'messages':[{'role':'user','content':'synthetic performance probe'}],
                   'max_tokens':32,'stream':stream}
        if stream: payload['stream_options'] = {'include_usage':True}
        self.body = json.dumps(payload).encode()
        if profiles is None:
            profiles = [{'model': model, 'api': 'openai'} for _ in keys]
        if len(profiles) != len(keys) or any(not isinstance(p, dict) or
                not {'model', 'api'} <= set(p) or set(p)-{'model','api','workload'} or p['api'] not in ('openai','anthropic') or
                not isinstance(p['model'],str) or not 1 <= len(p['model']) <= 256 for p in profiles):
            raise ValueError('invalid mock protocol profiles')
        self.profiles = tuple(dict(p) for p in profiles)
        self.bodies = []
        for profile in self.profiles:
            body = dict(payload, model=profile['model'])
            if 'workload' in profile:
                work=profile['workload']
                limits={'input_tokens':(128,32768),'output_tokens':(32,4096),'ttft_ms':(0,2000),'chunk_ms':(0,500)}
                if not isinstance(work,dict) or set(work)!=set(limits)|{'task'} or any(
                        type(work[k]) is not int or not lo<=work[k]<=hi for k,(lo,hi) in limits.items()) or not isinstance(work['task'],str) or len(work['task'])>512:
                    raise ValueError('Invalid synthetic workload bounds')
                instruction={k:work[k] for k in limits}
                body['messages']=[{'role':'user','content':'ENMAAS_MOCK_WORKLOAD '+json.dumps(instruction)+'\n'+work['task']}]
                body['max_tokens']=work['output_tokens']
            if profile['api'] == 'anthropic': body.pop('stream_options', None)
            self.bodies.append(json.dumps(body).encode())
        self.condition = threading.Condition()
        self.pending = {}
        self.closed = False
        self.watchdog = threading.Thread(target=self._watch, daemon=True) if watchdog else None
        if self.watchdog is not None:self.watchdog.start()

    def _watch(self):
        with self.condition:
            while not self.closed:
                now = time.monotonic()
                expired = [key for key, value in self.pending.items() if value['deadline'] <= now]
                for key in expired:
                    entry = self.pending.pop(key)
                    entry['expired'].set()
                    try: entry['socket'].shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                delay = min((v['deadline']-now for v in self.pending.values()), default=1)
                self.condition.wait(max(.001,delay))

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def close(self):
        with self.condition:
            self.closed = True
            for entry in self.pending.values():
                entry['expired'].set()
                try: entry['socket'].shutdown(socket.SHUT_RDWR)
                except OSError: pass
            self.condition.notify_all()
        if self.watchdog is not None:self.watchdog.join(timeout=2)

    def __call__(self, user, timeout):
        if type(user) is not int or not 0 <= user < len(self.keys):
            raise ValueError('invalid benchmark user index')
        if not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('invalid request deadline')
        started = time.monotonic()
        deadline = started+timeout
        expired = threading.Event()
        identifier = object()
        conn = http.client.HTTPSConnection(self.hostname,self.port,context=self.context,timeout=timeout)
        raw = socket.socket(socket.AF_INET6 if self.address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(timeout)
        tls = None
        response = None
        try:
            raw.connect((str(self.address), self.port))
            raw.settimeout(max(.001,deadline-time.monotonic()))
            tls = self.context.wrap_socket(raw,server_hostname=self.hostname,do_handshake_on_connect=False)
            with self.condition:
                if self.closed: raise RuntimeError('transport is closed')
                self.pending[identifier] = {'deadline':deadline,'socket':tls,'expired':expired}
                self.condition.notify_all()
            tls.do_handshake()
            conn.sock = tls
            api = self.profiles[user]['api']
            headers = {'Content-Type':'application/json','User-Agent':self.user_agents[user]}
            if api == 'anthropic':
                headers.update({'x-api-key':self.keys[user], 'anthropic-version':'2023-06-01'})
            else: headers['Authorization'] = 'Bearer '+self.keys[user]
            conn.request('POST', '/v1/messages' if api == 'anthropic' else '/v1/chat/completions',
                         self.bodies[user], headers)
            response = conn.getresponse()
            if response.status not in (200,429):
                # In particular, never follow redirects or retain their body.
                raise UnexpectedResponse('unexpected mock gateway response')
            chunks = []; size = 0; first_byte = None
            while True:
                chunk = response.read1(min(65536,MAX_BODY+1-size))
                if not chunk: break
                if first_byte is None: first_byte = time.monotonic()-started
                size += len(chunk)
                if size>MAX_BODY: raise InvalidReply('response size limit exceeded')
                chunks.append(chunk)
            if expired.is_set() or time.monotonic() >= deadline:
                raise RequestTimeout('request deadline exceeded')
            data = b''.join(chunks)
            if response.status == 200:
                work=self.profiles[user].get('workload',{'input_tokens':2,'output_tokens':3})
                self.validate(data, api, (work['input_tokens'],work['output_tokens']))
            elif b'budget' in data.lower() and not self.allow_budget_denials:
                raise UnexpectedResponse('budget denial is not an expected transport-rate-limit result')
            return {'status':response.status,'bytes':size,'first_byte_seconds':first_byte,
                    'denial_reason':('budget' if b'budget' in data.lower() else 'rate_limit') if response.status==429 else None}
        except Exception:
            if expired.is_set() or time.monotonic() >= deadline:
                raise RequestTimeout('request deadline exceeded') from None
            raise
        finally:
            with self.condition:
                self.pending.pop(identifier,None)
                self.condition.notify_all()
            if response is not None: response.close()
            conn.close()
            if tls is not None: tls.close()
            raw.close()

    def validate(self, data, api="openai", expected=(2,3)):
        try:
            if api == 'anthropic':
                if self.stream:
                    events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b'data: ')]
                    if not events or events[-1].get('type') != 'message_stop': raise ValueError()
                    first = next(e['message']['usage'] for e in events if e.get('type') == 'message_start')
                    last = next(e['usage'] for e in reversed(events) if e.get('type') == 'message_delta')
                    counts = (first.get('input_tokens'), last.get('output_tokens'))
                else:
                    usage = json.loads(data)['usage']
                    counts = (usage.get('input_tokens'), usage.get('output_tokens'))
                if any(type(v) is not int for v in counts) or counts != expected: raise ValueError()
                return
            if self.stream:
                lines = [line[6:] for line in data.splitlines() if line.startswith(b'data: ')]
                if not lines or lines[-1] != b'[DONE]': raise ValueError()
                events = [json.loads(line) for line in lines[:-1]]
                usage = next(event['usage'] for event in reversed(events) if event.get('usage'))
            else:
                usage = json.loads(data)['usage']
            if any(type(usage.get(key)) is not int or usage[key] != value for key,value in
                   (('prompt_tokens',expected[0]),('completion_tokens',expected[1]),('total_tokens',sum(expected)))):
                raise ValueError()
        except (ValueError,KeyError,TypeError,StopIteration,AttributeError):
            raise InvalidReply('mock usage or stream framing mismatch') from None
