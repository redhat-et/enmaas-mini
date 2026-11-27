"""IAM policy packing preserves grants and fails before exceeding document quotas."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('policy_pack',(Path(__file__).resolve().parents[1] / 'policy-pack.py'))
pack=importlib.util.module_from_spec(spec);spec.loader.exec_module(pack)


class Packing(unittest.TestCase):
    def document(self,count):
        return {'Version':'2012-10-17','Statement':[{'Sid':'Grant'+str(i),'Effect':'Allow',
                'Action':['ec2:DescribeVpcs'],'Resource':['*']} for i in range(count)]}

    def test_pack_keeps_statements_and_order_without_mutating_inputs(self):
        documents=[self.document(3),self.document(2)];before=copy.deepcopy(documents)
        result=pack.pack(documents,max_chars=260)
        self.assertGreater(len(result),1)
        self.assertTrue(all(len(json.dumps(doc,separators=(',',':')))<=260 for doc in result))
        original=[{k:v for k,v in s.items() if k!='Sid'} for d in documents for s in d['Statement']]
        actual=[{k:v for k,v in s.items() if k!='Sid'} for d in result for s in d['Statement']]
        self.assertEqual(original,actual)
        self.assertEqual(documents,before)
        sids=[s['Sid'] for d in result for s in d['Statement']]
        self.assertEqual(len(sids),len(set(sids)))

    def test_rejects_oversized_statement_attachment_count_and_invalid_documents(self):
        for arguments in (([self.document(1)],{'max_chars':80}),
                          ([self.document(4)],{'max_chars':200,'max_policies':1}),
                          ([],{}),([{'Version':'wrong','Statement':[]}],{}),
                          ([{'Version':'2012-10-17','Statement':['invalid']}],{}),
                          ([self.document(0)],{}),([self.document(1)],{'max_chars':7000}),
                          ([self.document(1)],{'max_policies':0})):
            with self.assertRaises(ValueError):pack.pack(arguments[0],**arguments[1])

    def test_real_network_and_vm_grants_fit_managed_policy_limits(self):
        spec=importlib.util.spec_from_file_location('pack_vm',(Path(__file__).resolve().parents[1] / 'vm-policy.py'))
        vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)
        manifest=vm.network.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')
        documents=[vm.network.network_policy(manifest),vm.vm_policy(manifest,['ami-12345678'],['m7i.2xlarge'],150)]
        result=pack.pack(documents)
        self.assertLessEqual(len(result),10)
        self.assertEqual(sum(len(d['Statement']) for d in documents),sum(len(d['Statement']) for d in result))
        self.assertTrue(all(len(json.dumps(d,separators=(',',':')))<=6144 for d in result))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.json';path.write_text(json.dumps(self.document(1)))
            command=[sys.executable,str((Path(__file__).resolve().parents[1] / 'policy-pack.py')),str(path)]
            result=subprocess.run(command,check=True,capture_output=True,text=True)
            self.assertEqual(len(json.loads(result.stdout)['policies']),1)
            path.write_text('invalid')
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Policy packing stopped:',result.stderr)

if __name__=='__main__':unittest.main()
