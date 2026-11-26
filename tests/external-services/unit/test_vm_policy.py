"""VM policy condition tests; live IAM qualification remains separate."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('vm_policy',HERE/'vm-policy.py')
policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
spec=importlib.util.spec_from_file_location('policy_evaluation',HERE/'unit/test_network_policy.py')
evaluation=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluation)


class VmPolicy(unittest.TestCase):
    def setUp(self):
        self.manifest=policy.network.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')
        self.document=policy.vm_policy(self.manifest,['ami-12345678'],['m7i.2xlarge'],150)
        self.base='arn:aws:ec2:eu-central-1:123456789012:'

    def context(self):
        run=self.manifest['id']
        tags={'Name':run+'-gateway','ResourcePrefix':run+'-gateway','ManagedBy':'secure-single-server-test',
              'Scenario':'remote-gateway','enmaas:group':'demo','enmaas:run':run,'enmaas:purpose':'external-services-test'}
        return {'aws:RequestedRegion':'eu-central-1','aws:TagKeys':list(tags),
                **{'aws:RequestTag/'+k:v for k,v in tags.items()}}

    def test_instance_requires_imdsv2_approved_type_and_no_role(self):
        context=self.context()
        context.update({'ec2:InstanceType':'m7i.2xlarge','ec2:MetadataHttpTokens':'required',
                        'ec2:MetadataHttpEndpoint':'enabled','ec2:MetadataHttpPutResponseHopLimit':'1'})
        resource=self.base+'instance/i-12345678'
        self.assertTrue(evaluation.allowed(self.document,'ec2:RunInstances',resource,context))
        for key,value in (('ec2:InstanceType','p5.48xlarge'),('ec2:MetadataHttpTokens','optional'),
                          ('ec2:MetadataHttpPutResponseHopLimit','2'),('ec2:InstanceProfile','arn:aws:iam::123456789012:instance-profile/admin')):
            bad=dict(context);bad[key]=value
            self.assertFalse(evaluation.allowed(self.document,'ec2:RunInstances',resource,bad))

    def test_volume_requires_encryption_and_bounded_gp3_disk(self):
        context=self.context();context.update({'ec2:Encrypted':'true','ec2:VolumeType':'gp3','ec2:VolumeSize':'150'})
        resource=self.base+'volume/vol-12345678'
        self.assertTrue(evaluation.allowed(self.document,'ec2:RunInstances',resource,context))
        for key,value in (('ec2:Encrypted','false'),('ec2:VolumeType','io2'),('ec2:VolumeSize','151')):
            bad=dict(context);bad[key]=value
            self.assertFalse(evaluation.allowed(self.document,'ec2:RunInstances',resource,bad))

    def test_images_exact_network_owned_and_retagging_never_standalone(self):
        image='arn:aws:ec2:eu-central-1::image/ami-12345678'
        self.assertTrue(evaluation.allowed(self.document,'ec2:RunInstances',image,{'aws:RequestedRegion':'eu-central-1'}))
        self.assertFalse(evaluation.allowed(self.document,'ec2:RunInstances',image.replace('12345678','99999999'),self.context()))
        owned={'aws:RequestedRegion':'eu-central-1','aws:ResourceTag/enmaas:group':'demo',
               'aws:ResourceTag/enmaas:run':self.manifest['id'],'aws:ResourceTag/enmaas:purpose':'external-services-test'}
        for kind in ('subnet','security-group','key-pair'):
            resource=self.base+kind+'/'+(self.manifest['id']+'-gateway' if kind=='key-pair' else 'existing')
            self.assertTrue(evaluation.allowed(self.document,'ec2:RunInstances',resource,owned))
            self.assertFalse(evaluation.allowed(self.document,'ec2:RunInstances',resource,self.context()))
        for kind,action in (('instance','RunInstances'),('volume','RunInstances'),('network-interface','RunInstances'),
                            ('security-group','CreateSecurityGroup'),('key-pair','ImportKeyPair')):
            resource=self.base+kind+'/'+(self.manifest['id']+'-gateway' if kind=='key-pair' else 'new')
            context=self.context()
            self.assertFalse(evaluation.allowed(self.document,'ec2:CreateTags',resource,context))
            context['ec2:CreateAction']=action
            self.assertTrue(evaluation.allowed(self.document,'ec2:CreateTags',resource,context))
        for action in ('iam:PassRole','ec2:TerminateInstances','ec2:DeleteTags','ec2:CreateKeyPair','secretsmanager:GetSecretValue'):
            self.assertFalse(evaluation.allowed(self.document,action,self.base+'instance/existing',owned))

    def test_inputs_reject_wildcards_empty_lists_and_unreasonable_disk(self):
        for images,types,size in (([],['m7i.2xlarge'],150),(['ami-*'],['m7i.2xlarge'],150),
                                 (['ami-12345678'],[],150),(['ami-12345678'],['*'],150),
                                 (['ami-12345678'],['m7i.2xlarge'],0),(['ami-12345678'],['m7i.2xlarge'],20000)):
            with self.assertRaises(ValueError):policy.vm_policy(self.manifest,images,types,size)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=policy.network.registry.save(Path(tmp)/'registry',self.manifest)
            command=[sys.executable,str(HERE/'vm-policy.py'),str(path),'--image-id','ami-12345678',
                     '--instance-type','m7i.2xlarge','--max-volume-gib','150']
            self.assertEqual(json.loads(subprocess.run(command,check=True,capture_output=True,text=True).stdout),self.document)
            command[-1]='0'
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('VM policy stopped:',result.stderr)

if __name__=='__main__':unittest.main()
