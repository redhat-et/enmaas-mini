"""Network creation records intent before cloud writes and never retries uncertainty."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import io
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('vpc_create',(Path(__file__).resolve().parents[1] / 'vpc-create.py'))
vpc=importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpc)


class Creation(unittest.TestCase):
    def test_client_scopes_requests_disables_retries_and_redacts_failures(self):
        client=vpc.Aws(self.manifest())
        with patch.object(vpc.subprocess,'run',return_value=Mock(returncode=0,stdout='{}')) as run:
            self.assertEqual(client.call('ec2','create-vpc',CidrBlock='10.42.0.0/20'),{})
            command=run.call_args.args[0]
            self.assertEqual(json.loads(command[-1]),{'CidrBlock':'10.42.0.0/20'})
            self.assertEqual(run.call_args.kwargs['env']['AWS_MAX_ATTEMPTS'],'1')
            self.assertEqual(run.call_args.kwargs['env']['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'],'true')
            self.assertNotIn('AWS_ACCESS_KEY_ID',run.call_args.kwargs['env'])
            self.assertIn('scoped',command)
            self.assertIn('eu-central-1',command)
            self.assertNotIn('--no-paginate',command)
        for result in (Mock(returncode=1,stdout='secret',stderr='secret'),
                       Mock(returncode=0,stdout='invalid secret'),Mock(returncode=0,stdout='[]')):
            with patch.object(vpc.subprocess,'run',return_value=result),self.assertRaises(ValueError) as caught:
                client.call('ec2','create-vpc')
            self.assertNotIn('secret',str(caught.exception))
        for error in (OSError('secret'),subprocess.TimeoutExpired(['secret'],60)):
            with patch.object(vpc.subprocess,'run',side_effect=error),self.assertRaises(ValueError):
                client.call('ec2','create-vpc')
        with self.assertRaisesRegex(ValueError,'not allowed'):client.call('iam','create-user')
        with patch.object(vpc.planner.Aws,'call',return_value={'Vpcs':[]}) as read:
            self.assertEqual(client.call('ec2','describe-vpcs'),{'Vpcs':[]})
            read.assert_called_once_with('ec2','describe-vpcs')

    def test_waiters_are_bounded_read_only_and_reject_invalid_identifiers(self):
        client=vpc.Aws(self.manifest())
        with patch.object(vpc.subprocess,'run',return_value=Mock(returncode=0,stdout='')) as run:
            for kind,identifier in (('vpc','vpc-12345678'),('subnet','subnet-12345678')):
                client.wait(kind,identifier)
                self.assertIn(kind+'-available',run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs['timeout'],180)
        for kind,identifier in (('unknown','x'),('vpc','--anything')):
            with self.assertRaises(ValueError):client.wait(kind,identifier)
        with patch.object(vpc.subprocess,'run',return_value=Mock(returncode=1)),self.assertRaises(ValueError):
            client.wait('vpc','vpc-12345678')

    def test_pending_vpc_stops_before_subsequent_mutation(self):
        self.serial=0;aws=Mock();aws.call.side_effect=self.response
        aws.wait.side_effect=ValueError('not ready')
        with tempfile.TemporaryDirectory() as tmp:
            journal=Path(tmp)/'network'
            with self.assertRaisesRegex(ValueError,'not ready'):self.run_create(journal,aws)
            self.assertEqual(aws.call.call_count,4)
            events=[json.loads(p.read_text()) for p in sorted(journal.glob('*.json'))]
            self.assertEqual(events[-1]['action'],'create-vpc')
            self.assertEqual(events[-1]['event'],'result')

    def test_cli_dry_run_and_apply(self):
        manifest=self.manifest()
        base=['vpc-create','--manifest','unused','--cidr','10.42.0.0/20',
              '--zone','eu-central-1a','--zone','eu-central-1b']
        with patch.object(sys,'argv',base),patch.object(vpc.planner.registry,'load',return_value=manifest), \
                patch.object(vpc,'Aws') as client,patch('sys.stdout',new_callable=io.StringIO) as output:
            vpc.main()
            self.assertFalse(json.loads(output.getvalue())['changes_made'])
            client.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            self.serial=0
            argv=base+['--apply','--journal',str(Path(tmp)/'network'),'--confirm','create network '+manifest['id']]
            with patch.object(sys,'argv',argv),patch.object(vpc.planner.registry,'load',return_value=manifest), \
                    patch.object(vpc,'Aws') as client,patch('sys.stdout',new_callable=io.StringIO) as output:
                client.return_value.call.side_effect=self.response
                vpc.main()
                self.assertIn('resources',json.loads(output.getvalue()))
        with patch.object(sys,'argv',base+['--apply']),patch.object(vpc.planner.registry,'load',return_value=manifest), \
                self.assertRaises(ValueError):vpc.main()

    def test_cli_reports_error_without_traceback(self):
        result=subprocess.run([sys.executable,str((Path(__file__).resolve().parents[1] / 'vpc-create.py')),
                               '--manifest','missing','--cidr','10.42.0.0/20','--zone','eu-central-1a'],
                              capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Network creation stopped:',result.stderr)
        self.assertNotIn('Traceback',result.stderr)
    def manifest(self):
        return vpc.planner.registry.new('123456789012','eu-central-1','/tmp/key','scoped',group='demo')

    def response(self,service,action,**options):
        if service=='sts':return {'Account':'123456789012','Arn':'arn:aws:sts::123456789012:assumed-role/em-demo-operator/enmaas-demo'}
        if action=='describe-availability-zones':return {'AvailabilityZones':[
            {'ZoneName':'eu-central-1'+suffix,'ZoneId':'euc1-az'+str(i),'RegionName':'eu-central-1',
             'State':'available','ZoneType':'availability-zone','OptInStatus':'opt-in-not-required'}
            for i,suffix in enumerate(('a','b'),1)]}
        if action=='describe-vpcs':return {'Vpcs':[]}
        ids={'create-vpc':('Vpc','VpcId','vpc'), 'create-internet-gateway':('InternetGateway','InternetGatewayId','igw'),
             'create-route-table':('RouteTable','RouteTableId','rtb'), 'create-subnet':('Subnet','SubnetId','subnet')}
        if action in ids:
            container,field,prefix=ids[action]
            self.serial+=1
            return {container:{field:prefix+'-'+format(self.serial,'08x')}}
        if action=='associate-route-table':
            self.serial+=1
            return {'AssociationId':'rtbassoc-'+format(self.serial,'08x')}
        if action=='create-route':return {'Return':True}
        return {}

    def run_create(self,root,aws,manifest=None):
        manifest=manifest or self.manifest()
        return vpc.create(aws,manifest,'10.42.0.0/20',['eu-central-1a','eu-central-1b'],root,
                          'create network '+manifest['id'])

    def test_success_tags_creation_and_keeps_database_routes_private(self):
        self.serial=0;aws=Mock();aws.call.side_effect=self.response
        with tempfile.TemporaryDirectory() as tmp:
            journal=Path(tmp)/'network'
            result=self.run_create(journal,aws)
            self.assertEqual(set(result['subnets']),{'services-1','services-2','database-1','database-2'})
            self.assertEqual(len(result['associations']),4)
            events=[json.loads(p.read_text()) for p in sorted(journal.glob('*.json'))]
            self.assertEqual(events[-1]['event'],'complete')
            self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in journal.glob('*.json')))
            self.assertEqual(journal.stat().st_mode&0o777,0o700)
            intents=[e for e in events if e['event']=='intent']
            successes=[e for e in events if e['event']=='result']
            self.assertEqual(len(intents),len(successes))
            calls=aws.call.call_args_list[3:]
            for call in calls:
                if call.args[1] in ('create-vpc','create-internet-gateway','create-route-table','create-subnet'):
                    tags={t['Key']:t['Value'] for t in call.kwargs['TagSpecifications'][0]['Tags']}
                    self.assertEqual(tags['enmaas:group'],'demo')
                    self.assertTrue(tags['enmaas:run'].startswith('em-demo-'))
            routes=[c for c in calls if c.args[1]=='create-route']
            self.assertEqual(len(routes),1)
            self.assertEqual(routes[0].kwargs['RouteTableId'],result['route_tables']['public'])
            self.assertEqual(routes[0].kwargs['DestinationCidrBlock'],'0.0.0.0/0')
            self.assertFalse(any(c.kwargs.get('MapPublicIpOnLaunch',{}).get('Value',False) for c in calls))
            for intent in intents:
                self.assertEqual(intent['event'],'intent')
            with self.assertRaises(FileExistsError):self.run_create(journal,aws)

    def test_denied_confirmation_or_preflight_never_creates_journal(self):
        aws=Mock();manifest=self.manifest()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'network'
            with self.assertRaisesRegex(ValueError,'confirmation'):
                vpc.create(aws,manifest,'10.42.0.0/20',['eu-central-1a','eu-central-1b'],path,'yes')
            aws.call.assert_not_called()
            aws.call.side_effect=ValueError('denied')
            with self.assertRaises(ValueError):self.run_create(path,aws)
            self.assertFalse(path.exists())

    def test_failure_at_each_mutation_retains_intent_and_stops(self):
        # Establish the full operation count without coupling to an internal constant.
        self.serial=0;aws=Mock();aws.call.side_effect=self.response
        with tempfile.TemporaryDirectory() as tmp:self.run_create(Path(tmp)/'ok',aws)
        mutations=len(aws.call.call_args_list)-3
        for failure in range(mutations):
            self.serial=0;aws=Mock();counter=0
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'network'
                def reply(service,action,**options):
                    nonlocal counter
                    if action not in ('get-caller-identity','describe-availability-zones','describe-vpcs'):
                        events=[json.loads(p.read_text()) for p in sorted(path.glob('*.json'))]
                        self.assertEqual(events[-1]['event'],'intent')
                        if counter==failure:raise TimeoutError('sensitive diagnostic')
                        counter+=1
                    return self.response(service,action,**options)
                aws.call.side_effect=reply
                with self.subTest(operation=failure),self.assertRaisesRegex(ValueError,'uncertain') as caught:
                    self.run_create(path,aws)
                self.assertNotIn('sensitive',str(caught.exception))
                self.assertEqual(len(aws.call.call_args_list),failure+4)
                events=[json.loads(p.read_text()) for p in sorted(path.glob('*.json'))]
                self.assertEqual(events[-1]['event'],'intent')
                self.assertNotIn('sensitive',json.dumps(events))

    def test_invalid_resource_response_stops_without_recording_success(self):
        self.serial=0;aws=Mock()
        def reply(service,action,**options):
            if action=='create-vpc':return {'Vpc':{'VpcId':'not-an-id'}}
            return self.response(service,action,**options)
        aws.call.side_effect=reply
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'network'
            with self.assertRaisesRegex(ValueError,'uncertain'):self.run_create(path,aws)
            self.assertEqual(len(aws.call.call_args_list),4)
            self.assertEqual(json.loads(sorted(path.glob('*.json'))[-1].read_text())['event'],'intent')

    def test_unsafe_journal_parent_and_failed_route_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);public=root/'public';public.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError,'private'):vpc.Journal(public/'journal')
            link=root/'link';link.symlink_to(root,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'private'):vpc.Journal(link/'journal')
            self.serial=0;aws=Mock()
            def reply(service,action,**options):
                if action=='create-route':return {'Return':False}
                return self.response(service,action,**options)
            aws.call.side_effect=reply
            with self.assertRaisesRegex(ValueError,'uncertain'):self.run_create(root/'network',aws)
            self.assertEqual(aws.call.call_args.args[1],'create-route')

if __name__=='__main__':unittest.main()
