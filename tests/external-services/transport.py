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
    def __init__(self, url, ca, keys, model, stream=True):
        origin = urlsplit(url)
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path not in ('', '/') or origin.query or origin.fragment):
            raise ValueError('require an HTTPS origin without credentials or path')
        self.hostname = origin.hostname
        self.address = ipaddress.ip_address('127.0.0.1' if origin.hostname == 'localhost' else origin.hostname)
        self.port = origin.port or 443
        if not keys or any(not isinstance(key, str) or not key or len(key)>16384 or '\r' in key or '\n' in key for key in keys):
            raise ValueError('invalid benchmark credentials')
        if not isinstance(model,str) or not model or len(model)>256:
            raise ValueError('invalid mock model')
        self.keys = tuple(keys)
        self.context = ssl.create_default_context(cafile=str(ca))
        self.context.set_alpn_protocols(['http/1.1'])
        self.stream = stream
        payload = {'model':model, 'messages':[{'role':'user','content':'synthetic performance probe'}],
                   'max_tokens':32,'stream':stream}
        if stream: payload['stream_options'] = {'include_usage':True}
        self.body = json.dumps(payload).encode()
        self.condition = threading.Condition()
        self.pending = {}
        self.closed = False
        self.watchdog = threading.Thread(target=self._watch, daemon=True)
        self.watchdog.start()

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
        self.watchdog.join(timeout=2)

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
            conn.request('POST','/v1/chat/completions',self.body,
                         {'Authorization':'Bearer '+self.keys[user],'Content-Type':'application/json'})
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
                self.validate(data)
            elif b'budget' in data.lower():
                raise UnexpectedResponse('budget denial is not an expected transport-rate-limit result')
            return {'status':response.status,'bytes':size,'first_byte_seconds':first_byte}
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

    def validate(self, data):
        try:
            if self.stream:
                lines = [line[6:] for line in data.splitlines() if line.startswith(b'data: ')]
                if not lines or lines[-1] != b'[DONE]': raise ValueError()
                events = [json.loads(line) for line in lines[:-1]]
                usage = next(event['usage'] for event in reversed(events) if event.get('usage'))
            else:
                usage = json.loads(data)['usage']
            if any(type(usage.get(key)) is not int or usage[key] != value for key,value in
                   (('prompt_tokens',2),('completion_tokens',3),('total_tokens',5))):
                raise ValueError()
        except (ValueError,KeyError,TypeError,StopIteration,AttributeError):
            raise InvalidReply('mock usage or stream framing mismatch') from None
