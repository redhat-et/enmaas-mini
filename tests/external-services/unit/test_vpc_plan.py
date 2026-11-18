"""Dedicated-VPC plan isolates databases and never adopts existing networks."""
import importlib.util
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import copy
import io

spec=importlib.util.spec_from_file_location('vpc_plan',(Path(__file__).resolve().parents[1] / 'vpc-plan.py'))
vpc=importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpc)


class Plan(unittest.TestCase):
    def test_read_only_client_scopes_calls_and_redacts_errors(self):
        client=vpc.Aws(self.manifest())
        with patch.object(vpc.subprocess,'run',return_value=Mock(returncode=0,stdout='{"Vpcs": []}')) as run:
            self.assertEqual(client.call('ec2','describe-vpcs'),{'Vpcs':[]})
            command=run.call_args.args[0]
            self.assertIn('scoped',command)
            self.assertIn('eu-central-1',command)
            self.assertNotIn('--no-paginate',command)
            self.assertEqual(run.call_args.kwargs['timeout'],60)
            self.assertEqual(run.call_args.kwargs['env']['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
            self.assertNotIn('AWS_ACCESS_KEY_ID',run.call_args.kwargs['env'])
        with self.assertRaisesRegex(ValueError,'not allowed'):
            client.call('ec2','create-vpc')
        for response in (Mock(returncode=1,stdout='secret',stderr='credential secret'),
                         Mock(returncode=0,stdout='secret'), Mock(returncode=0,stdout='[]')):
            with patch.object(vpc.subprocess,'run',return_value=response),self.assertRaises(ValueError) as caught:
                client.call('sts','get-caller-identity')
            self.assertNotIn('secret',str(caught.exception))
        for error in (OSError('secret'),subprocess.TimeoutExpired(['secret'],60)):
            with patch.object(vpc.subprocess,'run',side_effect=error),self.assertRaises(ValueError) as caught:
                client.call('sts','get-caller-identity')
            self.assertNotIn('secret',str(caught.exception))

    def test_cli_live_preflight_uses_manifest_client(self):
        manifest=self.manifest()
        arguments=['vpc-plan','--manifest','unused','--cidr','10.42.0.0/20',
                   '--zone','eu-central-1a','--zone','eu-central-1b','--live']
        with patch.object(sys,'argv',arguments),patch.object(vpc.registry,'load',return_value=manifest), \
                patch.object(vpc,'Aws') as client,patch('sys.stdout',new_callable=io.StringIO) as output:
            client.return_value.call.side_effect=self.live_responses()
            vpc.main()
            self.assertTrue(json.loads(output.getvalue())['aws_validated'])
            client.assert_called_once_with(manifest)

    def test_cli_prints_plan_without_writing_files_and_fails_on_missing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=vpc.registry.save(root/'registry',self.manifest())
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            command=[sys.executable,str((Path(__file__).resolve().parents[1] / 'vpc-plan.py')),
                     '--manifest',str(path),'--cidr','10.42.0.0/20',
                     '--zone','eu-central-1a','--zone','eu-central-1b']
            result=subprocess.run(command,capture_output=True,text=True,check=True)
            self.assertFalse(json.loads(result.stdout)['changes_made'])
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})
            command[command.index('--manifest')+1]=str(root/'missing')
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('VPC plan stopped:',result.stderr)

    def manifest(self):
        return vpc.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')

    def test_two_or_three_zones_isolate_database_subnets(self):
        for zones in (['eu-central-1a','eu-central-1b'],['eu-central-1a','eu-central-1b','eu-central-1c']):
            with self.subTest(zones=zones):
                manifest=self.manifest()
                plan=vpc.build_plan(manifest,'10.42.0.0/20',zones)
                self.assertFalse(plan['changes_made'])
                self.assertFalse(plan['aws_validated'])
                self.assertEqual(plan['deployment'],manifest['id'])
                self.assertEqual(len(plan['subnets']),2*len(zones))
                networks=[ipaddress.ip_network(s['cidr']) for s in plan['subnets']]
                for i,left in enumerate(networks):
                    for right in networks[i+1:]:self.assertFalse(left.overlaps(right))
                private=[s for s in plan['subnets'] if s['purpose']=='database']
                self.assertEqual({s['zone'] for s in private},set(zones))
                self.assertTrue(all(s['route_table']=='private' for s in private))
                self.assertEqual(plan['route_tables']['private']['routes'],[])
                self.assertEqual(plan['route_tables']['public']['routes'],[{'destination':'0.0.0.0/0','target':'internet-gateway'}])
                self.assertTrue(all(not s['map_public_ip_on_launch'] for s in plan['subnets']))
                self.assertEqual(set(plan['placement']),{'gateway','keycloak','observability'})
                public={s['key'] for s in plan['subnets'] if s['purpose']=='services'}
                self.assertTrue(set(plan['placement'].values())<=public)
                tags={t['Key']:t['Value'] for t in plan['tags']}
                self.assertEqual(tags['enmaas:run'],manifest['id'])
                self.assertEqual(tags['enmaas:group'],'demo')
                self.assertFalse(plan['cleanup']['execution_enabled'])
                self.assertTrue(plan['cleanup']['reject_default_or_unjournaled_vpc'])

    def test_rejects_nonprivate_noncanonical_or_too_small_cidrs(self):
        for cidr in ('0.0.0.0/0','100.64.0.0/16','192.0.2.0/24','10.0.0.1/20','10.0.0.0/22','10.0.0.0/8','::/0','invalid'):
            with self.subTest(cidr=cidr),self.assertRaises(ValueError):
                vpc.build_plan(self.manifest(),cidr,['eu-central-1a','eu-central-1b'])

    def test_rejects_missing_duplicate_foreign_and_local_zones(self):
        for zones in ([],['eu-central-1a'],['eu-central-1a']*2,
                      ['eu-central-1a','us-east-1a'],['eu-central-1a','eu-central-1-ham-1a'],
                      ['eu-central-1a','eu-central-1b','eu-central-1c','eu-central-1d']):
            with self.subTest(zones=zones),self.assertRaises(ValueError):
                vpc.build_plan(self.manifest(),'10.42.0.0/20',zones)

    def test_legacy_manifest_does_not_silently_gain_owned_network(self):
        manifest=vpc.registry.new('123456789012','eu-central-1','/tmp/key','scoped')
        with self.assertRaisesRegex(ValueError,'group'):
            vpc.build_plan(manifest,'10.42.0.0/20',['eu-central-1a','eu-central-1b'])

    def test_plan_is_deterministic_and_does_not_mutate_manifest(self):
        import copy
        manifest=self.manifest();before=copy.deepcopy(manifest)
        first=vpc.build_plan(manifest,'172.20.0.0/16',['eu-central-1a','eu-central-1b'])
        self.assertEqual(first,vpc.build_plan(manifest,'172.20.0.0/16',['eu-central-1a','eu-central-1b']))
        self.assertEqual(manifest,before)
        self.assertEqual(first['main_route_table'],'local-only')
        self.assertEqual(first['vpc']['tenancy'],'default')
        self.assertNotIn('nat_gateway',first)


    def live_responses(self):
        return [
            {'Account':'123456789012','Arn':'arn:aws:sts::123456789012:assumed-role/em-demo-operator/enmaas-demo'},
            {'AvailabilityZones':[{'ZoneName':'eu-central-1'+suffix,'ZoneId':'euc1-az'+str(index),
                'RegionName':'eu-central-1','State':'available','ZoneType':'availability-zone',
                'OptInStatus':'opt-in-not-required'} for index,suffix in enumerate(('a','b'),1)]},
            {'Vpcs':[{'VpcId':'vpc-12345678','OwnerId':'123456789012','CidrBlock':'172.31.0.0/16',
                      'CidrBlockAssociationSet':[{'CidrBlock':'172.31.0.0/16','CidrBlockState':{'State':'associated'}}]}]},
        ]

    def test_rejects_unscoped_credentials_before_ec2_calls(self):
        for arn in (
            'arn:aws:iam::123456789012:user/operator',
            'arn:aws:sts::123456789012:assumed-role/Administrator/session',
            'arn:aws:sts::123456789012:assumed-role/em-other-operator/session',
            'arn:aws:sts::123456789012:federated-user/operator',
            'arn:aws:sts::123456789012:assumed-role/em-demo-operator/',
        ):
            aws=Mock();aws.call.return_value={'Account':'123456789012','Arn':arn}
            with self.subTest(arn=arn),self.assertRaisesRegex(ValueError,'operator role'):
                vpc.live_plan(aws,self.manifest(),'10.42.0.0/20',['eu-central-1a','eu-central-1b'])
            aws.call.assert_called_once_with('sts','get-caller-identity')

    def test_live_preflight_checks_all_addresses_and_keeps_capacity_unqualified(self):
        aws=Mock();aws.call.side_effect=self.live_responses()
        result=vpc.live_plan(aws,self.manifest(),'10.42.0.0/20',['eu-central-1a','eu-central-1b'])
        self.assertTrue(result['aws_validated'])
        self.assertFalse(result['changes_made'])
        self.assertEqual(result['zone_ids'],{'eu-central-1a':'euc1-az1','eu-central-1b':'euc1-az2'})
        self.assertIn('capacity',result['preflight_scope'])
        self.assertEqual([c.args for c in aws.call.call_args_list],[('sts','get-caller-identity'),
            ('ec2','describe-availability-zones'),('ec2','describe-vpcs')])

    def test_live_preflight_rejects_foreign_root_partial_or_unavailable_state(self):
        cases=('account','root','arn-missing','arn-account','zone-missing','zone-duplicate','zone-state','zone-type','zone-region',
               'zone-opt-in','zone-id','zone-id-duplicate','vpc-page','vpc-owner','primary-overlap',
               'secondary-overlap','run-collision','vpc-duplicate','zone-page')
        for case in cases:
            replies=self.live_responses()
            zones=replies[1]['AvailabilityZones'];network=replies[2]['Vpcs'][0]
            manifest=self.manifest()
            if case=='account':replies[0]['Account']='999999999999'
            if case=='root':replies[0]['Arn']='arn:aws:iam::123456789012:root'
            if case=='arn-missing':replies[0].pop('Arn')
            if case=='arn-account':replies[0]['Arn']='arn:aws:iam::999999999999:user/operator'
            if case=='zone-missing':zones.pop()
            if case=='zone-duplicate':zones.append(copy.deepcopy(zones[0]))
            if case=='zone-state':zones[0]['State']='unavailable'
            if case=='zone-type':zones[0]['ZoneType']='local-zone'
            if case=='zone-region':zones[0]['RegionName']='us-east-1'
            if case=='zone-opt-in':zones[0]['OptInStatus']='not-opted-in'
            if case=='zone-id':zones[0]['ZoneId']=''
            if case=='zone-id-duplicate':zones[0]['ZoneId']=zones[1]['ZoneId']
            if case=='vpc-page':replies[2]['NextToken']='unconsumed'
            if case=='zone-page':replies[1]['NextToken']='unconsumed'
            if case=='vpc-owner':network['OwnerId']='999999999999'
            if case=='primary-overlap':network['CidrBlock']='10.42.0.0/16'
            if case=='secondary-overlap':network['CidrBlockAssociationSet'].append({'CidrBlock':'10.42.0.0/20','CidrBlockState':{'State':'associating'}})
            if case=='run-collision':network['Tags']=[{'Key':'enmaas:run','Value':manifest['id']}]
            if case=='vpc-duplicate':replies[2]['Vpcs'].append(copy.deepcopy(network))
            aws=Mock();aws.call.side_effect=replies
            with self.subTest(case=case),self.assertRaises(ValueError):
                vpc.live_plan(aws,manifest,'10.42.0.0/20',['eu-central-1a','eu-central-1b'])

    def test_disassociated_secondary_cidr_does_not_block_new_network(self):
        replies=self.live_responses()
        replies[2]['Vpcs'][0]['CidrBlockAssociationSet'].append({'CidrBlock':'10.42.0.0/20','CidrBlockState':{'State':'disassociated'}})
        replies[1]['AvailabilityZones'][0]['OptInStatus']='opted-in'
        aws=Mock();aws.call.side_effect=replies
        self.assertTrue(vpc.live_plan(aws,self.manifest(),'10.42.0.0/20',['eu-central-1a','eu-central-1b'])['aws_validated'])

if __name__=='__main__':unittest.main()
