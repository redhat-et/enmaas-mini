"""Worker endpoints, input bounds and build context remain local and explicit."""
import importlib.util
import json
from pathlib import Path
import unittest
from generator_worker import parse, MAX_INPUT


class GeneratorWorker(unittest.TestCase):
    def document(self):
        return {'port':28443,'keys':['synthetic-key'],'model':'fixture','profiles':[],
                'user_agents':[],'phase':{'concurrency':1,'duration':65,'offered_rps':10,
                'minimum_seconds':10,'maximum_seconds':120,'timeout':150}}

    def test_accepts_offset_gateway_port_but_not_remote_destination(self):
        for port in (28443,44443,58443):
            self.assertEqual(parse(json.dumps(dict(self.document(),port=port)).encode())['port'],port)
        for change in ({'port':443},{'port':True},{'port':58444},{'url':'https://example.test'},
                       {'keys':[]},{'keys':['k']*2501}):
            with self.assertRaises(ValueError):parse(json.dumps(dict(self.document(),**change)).encode())

    def test_rejects_unbounded_input_and_deadlines(self):
        with self.assertRaises(ValueError):parse(b' '* (MAX_INPUT+1))
        for change in ({'duration':3601},{'maximum_seconds':121},{'timeout':60},{'concurrency':40001}):
            document=self.document();document['phase'].update(change)
            with self.assertRaises(ValueError):parse(json.dumps(document).encode())

    def test_build_context_is_allowlisted_and_runs_nonroot(self):
        path=Path(__file__).resolve().parents[1]/'build-stress-generator.py'
        spec=importlib.util.spec_from_file_location('generator_builder',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        files,image=module.inputs()
        self.assertEqual(set(files),{'Containerfile','requirements.txt','generator_worker.py',
            'async_scenarios.py','async_transport.py','parallel_results.py','transport.py'})
        recipe=files['Containerfile'].decode()
        self.assertIn('USER 65532:65532',recipe)
        self.assertNotIn('COPY .',recipe)
        self.assertTrue(all(line.startswith('COPY --chmod=0644 ') for line in recipe.splitlines() if line.startswith('COPY')))
        self.assertRegex(image,r'^localhost/enmaas-stress-generator:[a-f0-9]{16}$')


if __name__=='__main__':unittest.main()
