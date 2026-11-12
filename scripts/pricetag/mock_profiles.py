"""Deterministic test endpoints using the installer's real provider identities.

These records are synthetic transport fixtures, not upstream model metadata or
an entitlement check. Real replacement must use authenticated HTTPS discovery.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def configuration():
    presets = json.loads((ROOT / 'configs/pricetag/provider-models.json').read_text())
    selection = {
        'openai': {'openai': ('gpt-5.4-mini', 'gpt-6-luna')},
        'external': {'openai': ('gpt-5.4-mini', 'rits/zai-org/glm-5-3'),
                     'anthropic': ('claude-sonnet-5-5', 'rits/zai-org/glm-5-3')},
    }
    providers = [
        {'name': 'openai', 'urls': {'openai': 'http://pricetag-provider:18080/v1'},
         'messages_auth': 'x-api-key'},
        {'name': 'external', 'urls': {'openai': 'http://pricetag-provider:18180/v1',
                                    'anthropic': 'http://pricetag-provider:18181'},
         'messages_auth': 'x-api-key'},
    ]
    models = {}
    for provider, apis in selection.items():
        for api, names in apis.items():
            metadata = {row[0]: row[1:] for row in presets[api]}
            for name in names:
                if name not in metadata:
                    raise ValueError('Mock profile requires a reviewed model preset: ' + name)
                context, output = metadata[name]
                alias = provider + '/' + name
                model = models.setdefault(alias, {'id': alias, 'provider': provider,
                    'model': name, 'apis': [], 'context': context, 'output': output})
                model['apis'].append(api)
                model['context'] = min(model['context'], context)
                model['output'] = min(model['output'], output)
    return {'providers': providers, 'models': list(models.values())}


def provider_settings(document=None):
    document = configuration() if document is None else document
    result = {}
    for provider in document['providers']:
        name = provider['name']
        models = [m for m in document['models'] if m['provider'] == name]
        prices = {m['id']: m['model'] for m in models}
        result[name] = {'models': models, 'model': models[0], 'prices': prices,
            'price': models[0]['model'], 'urls': provider['urls'],
            'api': 'openai' if name == 'openai' else 'both',
            'url': provider['urls']['openai'], 'messages_auth': 'x-api-key',
            'mock': True, 'secret': 'pricetag-mock-' + name + '-v1'}
    return result


def save_inputs(helper):
    """Fresh mock install only; persist the normal provider replacement inputs."""
    from demo_config import native_inputs
    document = configuration()
    for name, settings in provider_settings(document).items():
        helper.save_provider(name, settings,
                             'synthetic-openai' if name == 'openai' else 'synthetic-anthropic')
    payloads = {'providers.json': native_inputs(document), 'models.json': document['models'],
                'prices.json': {m['id']: m['model'] for m in document['models']}}
    for filename, value in payloads.items():
        path = helper.STATE / filename
        with path.open('x') as stream:
            json.dump(value, stream, indent=2)
        path.chmod(0o600)
    path = helper.STATE / 'provider-secrets.container'
    with path.open('x') as stream:
        stream.write('[Container]\nSecret=pricetag-mock-openai-v1,type=env,target=OPENAI_API_KEY\n'
                     'Secret=pricetag-mock-external-v1,type=env,target=CUSTOM_EXTERNAL_API_KEY\n')
    path.chmod(0o600)
