"""Mock provider identities must be replaceable by the normal provider helper."""
import sys
import json
import tempfile
import importlib.machinery
import importlib.util
from unittest.mock import Mock, patch
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/pricetag'))
from mock_profiles import configuration, provider_settings, save_inputs
from demo_config import native_inputs


class MockProfiles(unittest.TestCase):
    def test_same_profile_names_and_aliases_as_real_installation(self):
        document = configuration()
        self.assertEqual({p['name'] for p in document['providers']}, {'openai', 'external'})
        for model in document['models']:
            self.assertEqual(model['id'], model['provider'] + '/' + model['model'])
            self.assertGreater(model['context'], model['output'])
        glm = next(m for m in document['models'] if m['model'] == 'rits/zai-org/glm-5-3')
        self.assertEqual(glm['apis'], ['openai', 'anthropic'])

    def test_native_routes_only_target_private_mock_servers(self):
        native = native_inputs(configuration())
        clusters = [c for chain in native['filter_chains'] for f in chain['filters']
                    if f['filter'] == 'load_balancer' for c in f['clusters']]
        self.assertEqual({c['name'] for c in clusters}, {'openai', 'external-openai', 'external-anthropic'})
        self.assertEqual({e for c in clusters for e in c['endpoints']},
                         {'pricetag-provider:18080', 'pricetag-provider:18180', 'pricetag-provider:18181'})
        credentials = [c for chain in native['filter_chains'] for f in chain['filters']
                       if f['filter'] == 'credential_injection' for c in f['clusters']]
        self.assertEqual({c['env_var'] for c in credentials}, {'OPENAI_API_KEY', 'CUSTOM_EXTERNAL_API_KEY'})

    def test_mock_ports_do_not_collide_with_private_praxis_listeners(self):
        from gateway import render
        document = configuration()
        result = render(native_inputs(document), document['models'])
        listeners = {int(item['address'].rsplit(':', 1)[1]) for item in result['listeners']}
        mock_ports = {18080, 18081, 18180, 18181, 19000, 19001}
        self.assertFalse(mock_ports & listeners)

    def test_saved_metadata_contains_replacement_contract_not_credential(self):
        for name, value in provider_settings().items():
            self.assertTrue(value['mock'])
            self.assertEqual(value['secret'], 'pricetag-mock-' + name + '-v1')
            self.assertNotIn('key', value)
            self.assertEqual(value['prices'], {m['id']: m['model'] for m in value['models']})

class ReplacementSafety(unittest.TestCase):
    def test_saved_inputs_use_the_normal_replacement_files(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Mock(STATE=Path(directory))
            save_inputs(helper)
            self.assertEqual(helper.save_provider.call_count, 2)
            self.assertEqual(json.loads((helper.STATE / 'models.json').read_text()), configuration()['models'])
            self.assertEqual((helper.STATE / 'provider-secrets.container').stat().st_mode & 0o777, 0o600)

    def test_external_refresh_replaces_mock_routes_preserving_other_profile(self):
        loader = importlib.machinery.SourceFileLoader('profile_providers', str(ROOT / 'scripts/pricetag/providers'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec); loader.exec_module(helper)
        from gateway import render
        document = configuration()
        original = native_inputs(document)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / 'state'; deploy = root / 'deploy'
            state.mkdir(); (deploy / 'gateway').mkdir(parents=True)
            (state / 'providers.json').write_text(json.dumps(original))
            (deploy / 'models.json').write_text(json.dumps(document['models']))
            (deploy / 'gateway/gateway.json').write_text(json.dumps(render(original, document['models'])))
            (deploy / 'price-sources.json').write_text(json.dumps({m['id']: m['model'] for m in document['models']}))
            settings = provider_settings()['external']
            settings['urls'] = {'openai': 'https://provider.example/v1', 'anthropic': 'https://messages.example'}
            settings['models'] = [m for m in settings['models'] if m['model'] != 'rits/zai-org/glm-5-3']
            with patch.object(helper, 'STATE', state), patch.object(helper, 'DEPLOY', deploy):
                files = helper.refresh_model_files(settings, name='external')
            result = json.loads(files[deploy / 'gateway/gateway.json'])
            endpoints = [e for c in result['filter_chains'] for f in c['filters']
                         if f['filter'] == 'load_balancer' for cluster in f['clusters'] for e in cluster['endpoints']]
            self.assertIn('pricetag-provider:18080', endpoints)
            self.assertIn('provider.example:443', endpoints)
            self.assertIn('messages.example:443', endpoints)
            self.assertNotIn('pricetag-provider:18180', endpoints)
            self.assertNotIn('pricetag-provider:18181', endpoints)
            models = json.loads(files[deploy / 'models.json'])
            self.assertFalse(any(m['model'] == 'rits/zai-org/glm-5-3' for m in models))

    def test_benchmark_rejects_real_credentials_and_remote_endpoint(self):
        loader = importlib.machinery.SourceFileLoader('profile_benchmark', str(ROOT / 'scripts/pricetag/benchmark'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        benchmark = importlib.util.module_from_spec(spec)
        loader.exec_module(benchmark)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'quadlets').mkdir(); (root / 'gateway').mkdir()
            (root / 'models.json').write_text(json.dumps(configuration()['models']))
            unit = ('Network=pricetag-mock.network\n'
                    'Secret=pricetag-mock-openai-v1,type=env,target=OPENAI_API_KEY\n'
                    'Secret=pricetag-mock-external-v1,type=env,target=CUSTOM_EXTERNAL_API_KEY\n')
            unit_path = root / 'quadlets/pricetag-gateway.container'
            config_path = root / 'gateway/gateway.json'
            unit_path.write_text(unit)
            config = native_inputs(configuration())
            config_path.write_text(json.dumps(config))
            benchmark.require_mock(root)
            unit_path.write_text(unit.replace('pricetag-mock-openai-v1', 'praxis-openai-api-key-v1'))
            with self.assertRaises(ValueError): benchmark.require_mock(root)
            unit_path.write_text(unit)
            config_path.write_text(json.dumps(config).replace('pricetag-provider:18080', 'api.example:443'))
            with self.assertRaises(ValueError): benchmark.require_mock(root)

if __name__ == '__main__':
    unittest.main()
