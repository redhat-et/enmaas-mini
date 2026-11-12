"""Interactive credential/discovery contract with synthetic keys and a fake AWS CLI."""
import errno
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT/'tests/external-services/aws-discover'


class Discovery(unittest.TestCase):
    def exercise(self, mode='normal', answers=None):
        answers = answers if answers is not None else ['synthetic-access-only', 'synthetic-secret-only', 'synthetic-token-only']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); cli=root/'aws'; calls=root/'calls.jsonl'
            cli.write_text('''#!/usr/bin/env python3
import json,os,sys
assert os.environ['AWS_ACCESS_KEY_ID']=='synthetic-access-only'
assert os.environ['AWS_SECRET_ACCESS_KEY']=='synthetic-secret-only'
assert os.environ['AWS_SESSION_TOKEN']=='synthetic-token-only'
assert 'AWS_PROFILE' not in os.environ and 'AWS_SECURITY_TOKEN' not in os.environ
assert os.environ['AWS_CONFIG_FILE']=='/dev/null'
assert os.environ['AWS_SHARED_CREDENTIALS_FILE']=='/dev/null'
assert os.environ['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS']=='true'
args=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as file:file.write(json.dumps(args)+'\\n')
if 'get-caller-identity' in args:
 if os.environ['TEST_MODE']=='identity-denied':sys.exit(1)
 print(json.dumps({'Account':'123456789012','Arn':'arn:aws:iam::123456789012:user/test'}))
elif 'list-hosted-zones' in args and os.environ['TEST_MODE']=='zone-denied':sys.exit(1)
else:print('[]')
''')
            cli.chmod(0o700)
            env={**os.environ,'PATH':str(root)+os.pathsep+os.environ['PATH'],
                 'TEST_CALLS':str(calls),'TEST_MODE':mode,'HOME':str(root),
                 'AWS_PROFILE':'old-profile','AWS_SECURITY_TOKEN':'stale-token'}
            child,terminal=pty.fork()
            if child==0:
                os.execvpe('bash',['bash','-x',str(SCRIPT),'eu-west-1'],env)
            output=b'';finished=False;index=0;deadline=time.monotonic()+10
            prompts=(b'AWS access key ID:',b'AWS secret access key:',b'AWS session token')
            try:
                while time.monotonic()<deadline:
                    if select.select([terminal],[],[],.05)[0]:
                        try:part=os.read(terminal,8192)
                        except OSError as error:
                            if error.errno==errno.EIO:break
                            raise
                        if not part:break
                        output+=part
                    if index<len(answers) and prompts[index] in output and not termios.tcgetattr(terminal)[3]&termios.ECHO:
                        answer=answers[index]
                        os.write(terminal,(answer if answer=='\x03' else answer+'\n').encode());index+=1
                while time.monotonic()<deadline:
                    pid,status=os.waitpid(child,os.WNOHANG)
                    if pid:finished=True;break
                    time.sleep(.01)
                self.assertTrue(finished,'interactive script did not finish')
            finally:
                if not finished:os.kill(child,signal.SIGKILL);os.waitpid(child,0)
                os.close(terminal)
            transcript=output.decode(errors='replace')
            for value in ('synthetic-access-only','synthetic-secret-only','synthetic-token-only','stale-token'):
                self.assertNotIn(value,transcript)
            self.assertFalse((root/'.aws').exists())
            recorded=[json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            return os.waitstatus_to_exitcode(status),transcript,recorded

    def test_hidden_credentials_read_only_calls_and_nonsecret_exports(self):
        code,text,calls=self.exercise()
        self.assertEqual(code,0,text)
        self.assertEqual(len(calls),6)
        self.assertIn('export ACCOUNT=123456789012',text)
        self.assertIn('export REGION=eu-west-1',text)
        for call,action in zip(calls,('get-caller-identity','list-hosted-zones','list-domains','describe-vpcs','describe-subnets','describe-instances')):
            self.assertIn(action,call)
            self.assertEqual(call[call.index('--region')+1],'us-east-1' if action=='list-domains' else 'eu-west-1')

    def test_permission_failure_is_partial_not_empty_success(self):
        code,text,calls=self.exercise('zone-denied')
        self.assertEqual(code,1);self.assertEqual(len(calls),6)
        self.assertIn('Discovery is partial',text)

    def test_identity_failure_or_cancel_stops_discovery(self):
        code,_,calls=self.exercise('identity-denied')
        self.assertEqual(code,1);self.assertEqual(len(calls),1)
        code,_,calls=self.exercise(answers=['\x03'])
        self.assertNotEqual(code,0);self.assertEqual(calls,[])

    def test_piped_credentials_are_refused(self):
        result=subprocess.run(['bash',str(SCRIPT),'eu-west-1'],input='synthetic\n',text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('synthetic',result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
