"""A platform-specific digest must never silently become the cross-host lock."""
import copy
import ast
import json
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('platforms',(Path(__file__).resolve().parents[1] / 'image-platforms.py'))
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class Platforms(unittest.TestCase):
    def test_issuer_proxy_lock_matches_gateway_installer(self):
        tree=ast.parse((p.HERE.parents[1]/'scripts/pricetag/install.py').read_text())
        constants={target.id:node.value.value for node in tree.body if isinstance(node,ast.Assign)
                   and isinstance(node.value,ast.Constant) for target in node.targets if isinstance(target,ast.Name)}
        image=json.loads((p.HERE/'images.json').read_text())['praxis']
        self.assertEqual(image['image'],constants['PRAXIS_IMAGE'])
        self.assertEqual(image['version'].split(':')[-1],'sha-'+constants['PRAXIS_REVISION'][:12])

    def index(self):
        return {'mediaType':'application/vnd.oci.image.index.v1+json','manifests':[
            {'digest':'sha256:'+str(i)*64,'platform':{'os':'linux','architecture':arch}}
            for i,arch in enumerate(('amd64','arm64'),1)]}
    def test_accepts_both_architectures_and_ignores_attestations(self):
        doc=self.index();doc['manifests'].append({'platform':{'os':'unknown','architecture':'unknown'}})
        self.assertEqual(set(p.validate_index(doc)),{'amd64','arm64'})
        doc['mediaType']='application/vnd.docker.distribution.manifest.list.v2+json'
        self.assertEqual(set(p.validate_index(doc)),{'amd64','arm64'})
    def test_rejects_child_missing_duplicate_and_bad_descriptors(self):
        for change in ('child','missing','duplicate','digest','descriptors'):
            doc=self.index()
            if change=='child':doc['mediaType']='application/vnd.oci.image.manifest.v1+json'
            if change=='missing':doc['manifests'].pop()
            if change=='duplicate':doc['manifests'].append(copy.deepcopy(doc['manifests'][0]))
            if change=='digest':doc['manifests'][0]['digest']='latest'
            if change=='descriptors':doc['manifests']=None
            with self.subTest(change=change),self.assertRaises(ValueError):p.validate_index(doc)
    def test_checker_uses_pinned_inspection_and_never_starts_containers(self):
        import json
        with patch.object(p.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(self.index()))) as run:
            p.main()
        self.assertEqual(run.call_count,9)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3],['podman','manifest','inspect'])
            self.assertIn('@sha256:',call.args[0][3]);self.assertEqual(call.kwargs['timeout'],60)
        with patch.object(p.subprocess,'run',return_value=Mock(returncode=1,stdout='secret')):
            with self.assertRaisesRegex(ValueError,'Cannot inspect'):p.main()

if __name__=='__main__':unittest.main()
