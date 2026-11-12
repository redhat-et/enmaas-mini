#!/usr/bin/env python3
"""Real local HTTPS tests; no provider credentials or external requests."""
import importlib.util
import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name+'.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

transport = load('transport')
local = load('local')

class Handler(BaseHTTPRequestHandler):
    mode = 'json'
    requests = 0
    def log_message(self, *args): pass
    def do_POST(self):
        type(self).requests += 1
        self.rfile.read(int(self.headers['Content-Length']))
        if self.mode == 'redirect':
            self.send_response(302); self.send_header('Location', '/other'); self.end_headers(); return
        if self.mode == 'rate':
            self.send_response(429); self.end_headers(); self.wfile.write(b'rate limited'); return
        if self.mode in ('slow','slow-headers'):
            if self.mode == 'slow':
                self.send_response(200); self.end_headers()
            else:
                self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: '); self.wfile.flush()
            try:
                for _ in range(100):
                    self.wfile.write(b' '); self.wfile.flush(); time.sleep(.02)
            except OSError: pass
            return
        body = {'usage': {'prompt_tokens':2, 'completion_tokens':3, 'total_tokens':5}}
        if self.mode == 'wrong': body['usage']['total_tokens'] = 500
        payload = json.dumps(body).encode()
        if self.mode == 'stream': payload = b'data: '+payload+b'\n\ndata: [DONE]\n\n'
        if self.mode == 'large': payload = b'x' * (1024*1024+1)
        self.send_response(200); self.send_header('Content-Length',str(len(payload))); self.end_headers()
        try: self.wfile.write(payload)
        except OSError: pass

class Transport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        local.issue_certificates(cls.root)
        cls.server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.root/'server.pem', cls.root/'server.key')
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = f'https://localhost:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.temp.cleanup()
    def setUp(self): Handler.requests = 0
    def request(self, mode, timeout=1):
        Handler.mode = mode
        with transport.MockTransport(self.url, self.root/'ca.pem', ['synthetic-key'], 'mock/fixture',
                                     stream=mode=='stream') as client:
            return client(0, timeout)
    def test_json_and_stream_usage(self):
        for mode in ('json','stream'):
            result = self.request(mode)
            self.assertEqual(result['status'], 200)
            self.assertGreater(result['bytes'], 0)
            self.assertNotIn('synthetic-key', str(result))
    def test_redirect_is_never_followed(self):
        with self.assertRaises(transport.UnexpectedResponse): self.request('redirect')
        self.assertEqual(Handler.requests,1)
    def test_wrong_usage_and_large_body_fail(self):
        for mode in ('wrong','large'):
            with self.assertRaises(transport.InvalidReply): self.request(mode)
    def test_rate_limit_remains_a_measured_response(self):
        self.assertEqual(self.request('rate')['status'],429)
    def test_slow_drip_obeys_wall_clock_deadline(self):
        for mode in ('slow','slow-headers'):
            started=time.monotonic()
            with self.assertRaises(transport.RequestTimeout): self.request(mode,.15)
            self.assertLess(time.monotonic()-started,.7)
    def test_scheduler_with_real_https_denials(self):
        scenarios = load('scenarios')
        Handler.mode = 'rate'
        with transport.MockTransport(self.url,self.root/'ca.pem',['key-a','key-b'],'mock/fixture') as client:
            report = scenarios.run_phase(client,users=2,lanes=1,duration=.2,offered_rps=20,timeout=.5)
        self.assertFalse(report['interrupted'])
        self.assertGreaterEqual(report['admission_seconds'],.19)
        self.assertGreaterEqual(report['attempts'],2)
        self.assertEqual(report['statuses'],{'429':report['attempts']})

    def test_transport_recovers_after_request_timeout(self):
        with transport.MockTransport(self.url,self.root/'ca.pem',['key'],'mock/fixture',stream=False) as client:
            Handler.mode = 'slow'
            with self.assertRaises(transport.RequestTimeout): client(0,.1)
            Handler.mode = 'json'
            self.assertEqual(client(0,1)['status'],200)

    def test_bad_ca_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as other:
            root=Path(other); local.issue_certificates(root)
            with transport.MockTransport(self.url,root/'ca.pem',['key'],'mock/fixture') as client:
                with self.assertRaises(ssl.SSLCertVerificationError): client(0,1)
    def test_only_ip_or_loopback_origins(self):
        for url in ('http://localhost', 'https://user:key@127.0.0.1', 'https://api.example.test',
                    'https://127.0.0.1/path','https://127.0.0.1/?secret=x'):
            with self.assertRaises(ValueError):
                transport.MockTransport(url,self.root/'ca.pem',['key'],'mock/fixture')

if __name__ == '__main__': unittest.main()
