#!/usr/bin/env python3
"""The common native-API fixture with vLLM's no-upstream-credential contract."""
import os
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, '/fixtures')
from provider import Provider

if os.environ.get('MOCK_PROFILES') == '1':
    document = json.loads(Path('/fixtures/mock-profiles.json').read_text())
    servers = []
    for name, ports, key in (('openai', (18080, 18081, 19000), 'synthetic-openai'),
                             ('external', (18180, 18181, 19001), 'synthetic-anthropic')):
        models = {api: [m['model'] for m in document['models']
                       if m['provider'] == name and api in m['apis']]
                  for api in ('openai', 'anthropic')}
        server = Provider(ports=ports, host='0.0.0.0', local=False,
            openai_authorization='Bearer ' + key, models_by_api=models,
            declared_tools_only=True, request_backlog=512, record_limit=1000)
        if 'MOCK_STREAM_DELAY' in os.environ:
            server.mode = 'delayed'
            server.delay = float(os.environ['MOCK_STREAM_DELAY'])
        server.start()
        servers.append(server)
    threading.Event().wait()

managed = os.environ.get('MOCK_MANAGEMENT') == '1'
fixture = (Provider(host='0.0.0.0', local=False, openai_authorization='Bearer synthetic-anthropic',
                    declared_tools_only=True, request_backlog=512, record_limit=1000)
           if managed else Provider(host='0.0.0.0', local=True, request_backlog=512))
if 'MOCK_STREAM_DELAY' in os.environ:
    fixture.mode = 'delayed'
    fixture.delay = float(os.environ['MOCK_STREAM_DELAY'])
fixture.start()
threading.Event().wait()
