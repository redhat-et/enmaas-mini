#!/usr/bin/env python3
"""The gateway agent must not expose receivers or send telemetry without TLS."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import yaml

spec=importlib.util.spec_from_file_location('agent',Path(__file__).with_name('telemetry-agent.py'))
agent=importlib.util.module_from_spec(spec);spec.loader.exec_module(agent)

class Agent(unittest.TestCase):
    def test_private_receivers_and_verified_client_identity(self):
        config=agent.configuration('10.0.0.20')
        self.assertEqual(config['receivers']['otlp']['protocols']['grpc']['endpoint'],'127.0.0.1:4317')
        export=config['exporters']['otlphttp/remote']
        self.assertEqual(export['endpoint'],'https://10.0.0.20:24443')
        self.assertNotIn('insecure',export['tls'])
        self.assertNotIn('insecure_skip_verify',export['tls'])
        self.assertEqual(export['sending_queue']['queue_size'],256)
        self.assertEqual(set(config['service']['pipelines']),{'metrics','traces','logs'})
        statements=str(config['processors']['transform']['metric_statements'])
        for label in ('cluster','route','listener','endpoint','method','status_class','filter','phase','quantile'):
            self.assertIn(chr(34)+label+chr(34),statements)
        jobs=config['receivers']['prometheus']['config']['scrape_configs']
        self.assertEqual([j['static_configs'][0]['targets'] for j in jobs],
                         [['127.0.0.1:9901'],['pricetag-metering:9902']])

    def test_log_access_and_size_are_explicitly_bounded(self):
        config=agent.configuration('10.0.0.20')
        receivers=config['service']['pipelines']['logs']['receivers']
        self.assertEqual(len(receivers),3)
        for receiver in receivers:
            settings=config['receivers'][receiver]
            self.assertEqual(settings['max_log_size'],'64KiB')
            self.assertEqual(settings['start_at'],'end')
            self.assertFalse(settings['include_file_path'])
            self.assertNotIn('*',settings['include'][0])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);agent.configure(root,'10.0.0.20')
            for name in ('pricetag-gateway','pricetag-metering','pricetag-maas'):
                dropin=(root/(name+'-logs.conf')).read_text()
                self.assertIn('LogOpt=max-size=10mb',dropin)
                self.assertIn(name+'.log',dropin)
            self.assertFalse((root/'systemd/enmaas-journal-logs.service').exists())

    def test_no_arbitrary_endpoint_or_cleartext_transport(self):
        for value in ('http://example.test','example.test','10.0.0.20:443','127.0.0.1','0.0.0.0','224.0.0.1','169.254.1.2','192.0.2.20'):
            with self.assertRaises(ValueError):agent.configuration(value)

    def test_rootless_unit_shares_only_gateway_network(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);agent.configure(root,'10.0.0.20')
            unit=(root/'quadlets/lab-gateway-telemetry.container').read_text()
            self.assertIn('Network=container:pricetag-gateway',unit)
            self.assertIn('DropCapability=all',unit)
            self.assertIn('NoNewPrivileges=true',unit)
            self.assertNotIn('PublishPort',unit)
            self.assertIn('Volume=%h/.local/share/enmaas-telemetry-logs:/logs:ro,z',unit)
            self.assertIn('User=0:0',unit)
            self.assertNotIn('/var/log/journal',unit)
            self.assertNotIn('privileged',unit.lower())
            sampler=(root/'systemd/enmaas-host-metrics.service').read_text()
            self.assertIn('User=praxis-svc',sampler)
            self.assertIn('LoadCredential=client.key:',sampler)
            self.assertIn('IPAddressDeny=any',sampler)
            self.assertIn('IPAddressAllow=10.0.0.20/32',sampler)
            self.assertNotIn('Environment=PASSWORD',sampler)
            self.assertEqual(yaml.safe_load((root/'collector.yaml').read_text()),agent.configuration('10.0.0.20'))

if __name__=='__main__':unittest.main()
