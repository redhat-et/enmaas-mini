#!/usr/bin/env python3
"""Run unchanged remote secret-file scripts in disposable, networkless containers."""
import importlib.util
from pathlib import Path
import secrets
import subprocess

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('transfer_runtime_client', HERE/'transfer-client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
PYTHON = 'docker.io/library/python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
CHECK = r'''
import json,os,stat,subprocess,sys,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);dest=root/'secret';sentinel=b'synthetic-transfer-probe'
 def run(script,path,data=None,extra=()):
  result=subprocess.run([sys.executable,'-c',script,str(path),*extra],input=data,capture_output=True,timeout=10)
  assert sentinel not in result.stderr
  return result
 if os.geteuid()!=0:
  assert run(WRITE,dest,sentinel).returncode!=0
  dest.write_bytes(sentinel);dest.chmod(0o600)
  result=run(READ,dest);assert result.returncode!=0 and not result.stdout
  result=run(BUNDLE_WRITE,root/'tls',json.dumps({'server.key':'leaf'}).encode(),(json.dumps(['server.key']),))
  assert result.returncode!=0 and not result.stdout
 else:
  result=run(WRITE,dest,sentinel);assert result.returncode==0 and not result.stdout
  assert dest.stat().st_uid==0 and stat.S_IMODE(dest.stat().st_mode)==0o600
  assert run(READ,dest).stdout==sentinel
  result=run(WRITE,dest,b'replacement');assert result.returncode!=0 and not result.stdout
  assert dest.read_bytes()==sentinel
  dest.chmod(0o644);result=run(READ,dest);assert result.returncode!=0 and not result.stdout
  dest.unlink();dest.symlink_to(root/'outside')
  assert run(WRITE,dest,sentinel).returncode!=0 and not (root/'outside').exists()
  result=run(READ,dest);assert result.returncode!=0 and not result.stdout
  names=['server.pem','server.key'];payload=json.dumps({name:'leaf' for name in names}).encode()
  tls=root/'tls';result=run(BUNDLE_WRITE,tls,payload,(json.dumps(names),))
  assert result.returncode==0 and not result.stdout
  assert stat.S_IMODE(tls.stat().st_mode)==0o700
  assert all(p.stat().st_uid==0 and stat.S_IMODE(p.stat().st_mode)==0o600 for p in tls.iterdir())
  assert run(BUNDLE_WRITE,tls,payload,(json.dumps(names),)).returncode!=0
  assert not list(root.glob('.client-*'))
print('PASS: root-only transfer contract' if os.geteuid()==0 else 'PASS: non-root transfer denied')
'''


def main():
    program='\n'.join(name+'='+repr(getattr(client,name)) for name in ('READ','WRITE','BUNDLE_WRITE'))+'\n'+CHECK
    prefix='enmaas-transfer-'+secrets.token_hex(6)
    names=[]
    try:
        for uid in ('0','1001'):
            name=prefix+'-'+uid;names.append(name)
            result=subprocess.run(['podman','run','--name',name,'--network','none',
                '--user',uid,'--read-only','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777',
                '--cap-drop','ALL','--security-opt','no-new-privileges',
                '--memory','128m','--pids-limit','64','-i',PYTHON,'python3','-'],
                input=program,text=True,capture_output=True,timeout=90)
            expected='PASS: root-only transfer contract' if uid=='0' else 'PASS: non-root transfer denied'
            if result.returncode or result.stdout.strip()!=expected:
                raise RuntimeError('Transfer filesystem runtime failed; captured output suppressed')
            print(expected,flush=True)
    finally:
        failed=[]
        for name in names:
            try:
                if subprocess.run(['podman','rm','--force','--ignore',name],capture_output=True,timeout=15).returncode:
                    failed.append(name)
            except (OSError,subprocess.TimeoutExpired):failed.append(name)
        if failed:raise RuntimeError('Transfer runtime cleanup failed; inspect '+','.join(failed))


if __name__=='__main__':
    main()
