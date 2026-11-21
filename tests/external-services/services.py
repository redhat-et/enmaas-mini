#!/usr/bin/env python3
"""Coordinate existing RHEL service installers on prepared, fingerprint-verified hosts.

Preview is offline. Apply fetches one reviewed commit on each host, never
provisions AWS, and never retries uncertain stages.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

HERE=Path(__file__).resolve().parent

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    loaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(loaded)
    return loaded

transfer=module('services_transfer',HERE/'transfer-client.py')
issuer=module('services_issuer',HERE/'issuer-config.py')
certificates=module('services_certificates',HERE/'certificates.py')
installer=module('services_installer',HERE.parents[1]/'scripts/pricetag/install.py')
state=transfer.population.state
ROLES=('keycloak','observability','gateway')
# Source revision qualified for OIDC and private management APIs; independent of
# the default published gateway image. Keep aligned with external-auth.md.
OIDC_METERING_COMMIT='2281e1a84bfc3b998e7db36bc01bae88130bfcdd'


def validate(config):
    fields={'version','deployment','revision','repository','ssh_key','known_hosts','tls_directory',
            'issuer_name','workstation_cidr','external_database','hosts','source_repository','population_count'}
    if not isinstance(config,dict) or set(config)!=fields or type(config['version']) is not int or config['version']!=1:
        raise ValueError('Unsupported service configuration')
    for name,pattern in (('deployment',r'em-[a-z][a-z0-9]{0,7}-[a-f0-9]{12}'),('revision',r'[a-f0-9]{40}')):
        if not isinstance(config[name],str) or not re.fullmatch(pattern,config[name]):raise ValueError('Invalid '+name)
    for name in ('repository','ssh_key','known_hosts','tls_directory'):
        value=config[name]
        if not isinstance(value,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',value) or '..' in Path(value).parts:
            raise ValueError('Use an absolute path without expansion or traversal: '+name)
    if config['repository']!='/opt/enmaas-mini/'+config['deployment']:
        raise ValueError('Repository must use the deployment-owned /opt/enmaas-mini directory')
    if (not isinstance(config['source_repository'],str)
            or not re.fullmatch(r'https://github[.]com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/enmaas-mini[.]git',config['source_repository'])):
        raise ValueError('Use a public GitHub enmaas-mini repository URL without credentials')
    if type(config['population_count']) is not int or not 1<=config['population_count']<=10000:
        raise ValueError('Choose 1–10000 synthetic users')
    if type(config['external_database']) is not bool:raise ValueError('external_database must be boolean')
    if not isinstance(config['workstation_cidr'],str):raise ValueError('Workstation CIDR must be a string')
    network=ipaddress.IPv4Network(config['workstation_cidr'],strict=True)
    if network.prefixlen<24:raise ValueError('Use a narrow workstation CIDR')
    hosts=config['hosts']
    if not isinstance(hosts,dict) or set(hosts)!=set(ROLES):raise ValueError('Specify exactly three service hosts')
    public=[];private=[]
    for role in ROLES:
        host=hosts[role]
        if not isinstance(host,dict) or set(host)!={'ssh','private_ip'}:raise ValueError('Invalid host fields')
        if not all(isinstance(host[key],str) for key in ('ssh','private_ip')):raise ValueError('Host addresses must be strings')
        public.append(transfer.target(host['ssh']).split('@')[1])
        address=ipaddress.IPv4Address(host['private_ip'])
        if not any(address in ipaddress.IPv4Network(cidr) for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
            raise ValueError('Service peers require RFC1918 private addresses')
        private.append(str(address))
    if len(set(public))!=3 or len(set(private))!=3:raise ValueError('Use three distinct hosts and private addresses')
    issuer.resolve(public[0],config['issuer_name'],private[0])
    return config


def plan(config):
    validate(config)
    hosts=config['hosts'];public={r:hosts[r]['ssh'].split('@')[1] for r in ROLES}
    private={r:hosts[r]['private_ip'] for r in ROLES}
    steps=[{'id':'local-tls','role':'workstation','kind':'generate-tls'}]
    def command(identity,role,*argv):steps.append({'id':identity,'role':role,'kind':'command','argv':list(argv)})
    for role in ROLES:
        command(role+'-packages',role,'dnf','install','-y','git','python3','python3-pyyaml','openssl')
        steps[-1]['outside_repository']=True
        command(role+'-checkout',role,'python3','-c',CHECKOUT,config['repository'],config['source_repository'],config['revision'])
        steps[-1]['outside_repository']=True
        steps.append({'id':role+'-tls','role':role,'kind':'tls'})
    identity_args=['--issuer',public['keycloak'],'--issuer-name',config['issuer_name'],'--issuer-private-ip',private['keycloak']]
    command('keycloak-prepare','keycloak','python3','tests/external-services/keycloak-host.py',*identity_args,
            '--gateway',public['gateway'],'--observability',public['observability'],'--output','/root/enmaas-keycloak-inputs')
    command('keycloak-install','keycloak','tests/external-services/install-keycloak','/root/enmaas-keycloak-inputs',
            '/root/enmaas-tls/server.pem','/root/enmaas-tls/server.key','/root/enmaas-tls/ca.pem',
            config['workstation_cidr'],private['gateway']+'/32',private['observability']+'/32')
    for role in ('observability','gateway'):
        command(role+'-inputs',role,'python3','-c',"import os; os.mkdir('/root/enmaas-inputs',0o700)")
        steps.append({'id':role+'-oidc','role':role,'kind':'client'})
    command('observability-prepare','observability','python3','tests/external-services/observability-host.py',*identity_args,
            '--observability',public['observability'],'--private-ip',private['observability'],
            '--client-secret-file','/root/enmaas-inputs/observability-client-secret','--output','/root/enmaas-observability-inputs')
    command('observability-install','observability','tests/external-services/install-observability',
            '/root/enmaas-observability-inputs','/root/enmaas-tls/server.pem','/root/enmaas-tls/server.key',
            '/root/enmaas-tls/issuer-ca.pem','/root/enmaas-tls/telemetry-ca.pem',public['observability'],
            private['gateway']+'/32',config['workstation_cidr'])
    command('gateway-issuer-trust','gateway','python3','-c',
            "from pathlib import Path; import os; data=Path('/root/enmaas-tls/issuer-ca.pem').read_bytes(); "
            "fd=os.open('/root/enmaas-inputs/issuer-ca.pem',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
            "f=os.fdopen(fd,'wb'); f.write(data); f.flush(); os.fsync(f.fileno()); f.close()")
    database=['--maas-database-url-file','/root/pricetag-external-db/maas.url',
              '--metering-database-url-file','/root/pricetag-external-db/metering.url',
              '--external-database-ca-file','/root/pricetag-external-db/ca.crt'] if config['external_database'] else []
    command('gateway-install','gateway','python3','scripts/pricetag/install.py','--non-interactive','--yes',
            '--mode','mock','--mock-profiles','--hostname',public['gateway'],'--no-sample-users','--monthly-usd','5',
            '--image-mode','source','--maas-revision',installer.MAAS_COMMIT,'--metering-revision',OIDC_METERING_COMMIT,
            '--tls-bundle','/root/enmaas-tls','--oidc-config','/root/enmaas-inputs/oidc.json',*database)
    command('telemetry-prepare','gateway','python3','tests/external-services/telemetry-agent.py',
            '--observability-private-ip',private['observability'],'--output','/root/enmaas-telemetry-inputs')
    command('telemetry-install','gateway','tests/external-services/install-telemetry-agent','/root/enmaas-telemetry-inputs',
            '/root/enmaas-tls/telemetry-server-ca.pem','/root/enmaas-tls/telemetry-client.pem','/root/enmaas-tls/telemetry-client.key')
    steps.append({'id':'personas-transfer','role':'gateway','kind':'client','client':'personas'})
    command('personas-configure','gateway','python3','tests/external-services/personas.py',
            '--identities','/root/enmaas-inputs/personas.json','--state','/root/enmaas-personas',
            '--origin','https://'+public['gateway']+':8443','--yes')
    command('population-create','keycloak','python3','tests/external-services/populate-keycloak.py',
            '--state','/root/enmaas-test-users','--count',str(config['population_count']),'--yes',
            '--export','/root/enmaas-test-users/profiles.json')
    steps.append({'id':'population-transfer','role':'gateway','kind':'client','client':'population'})
    command('population-import','gateway','python3','tests/external-services/import-population.py',
            '--profiles','/root/enmaas-inputs/population-profiles.json','--state','/root/enmaas-population-import','--yes')
    command('population-access','gateway','python3','tests/external-services/import-population.py',
            '--profiles','/root/enmaas-inputs/population-profiles.json','--state','/root/enmaas-population-import',
            '--configure-access','--monthly-usd','5','--key-lifetime','24h','--yes')
    return steps



def access_summary(config):
    """Offline connection instructions, not a readiness or authorization check."""
    validate(config)
    public={role:config['hosts'][role]['ssh'].split('@')[1] for role in ROLES}
    def instruction(role,*argv,interactive=False):
        # Display only. Execution uses transfer.ssh_options and validates private
        # local key/known-hosts files; do not relax host verification for copying.
        options=['ssh','-F','/dev/null','-i',config['ssh_key'],'-o','IdentitiesOnly=yes',
            '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','UserKnownHostsFile="'+config['known_hosts']+'"',
            '-o','GlobalKnownHostsFile=/dev/null','-o','UpdateHostKeys=no',
            '-o','ConnectTimeout=10','-o','ForwardAgent=no','-o','ClearAllForwardings=yes','-o','RequestTTY=yes' if interactive else 'RequestTTY=no']
        command='cd '+shlex.quote(config['repository'])+' && '+shlex.join(['sudo','-n',*argv])
        return shlex.join([*options,config['hosts'][role]['ssh'],command])
    return {
        'deployment':config['deployment'],'status':'Configured addresses; live readiness has not been checked',
        'urls':{'pricetag':'https://'+public['gateway']+':8443/login',
                'observability':'https://'+public['observability']+':8443/',
                'issuer':'https://'+config['issuer_name']+':8443/realms/enmaas-lab'},
        'public_ca_file':str(Path(config['tls_directory'])/'gateway'/'ca.pem'),
        'credential_notes':[
            'Password/key export commands explicitly print one secret when run. Use a protected pipe or private destination; never a recorded terminal.',
            'Keycloak passwords are browser credentials. MaaS keys are inference/API credentials.',
            'This summary contains no credential values and does not prove the services are installed.'],
        'browser_password_commands':{
            name:instruction('keycloak','python3','tests/external-services/keycloak-password.py','--user',name,'--stdout')
            for name in ('admin','alice','bob')},
        'population_user_1':{
            'metadata':instruction('gateway','python3','tests/external-services/population-key.py','--index','1'),
            'inference_key':instruction('gateway','python3','tests/external-services/population-key.py','--index','1','--stdout'),
            'browser_password':instruction('keycloak','python3','tests/external-services/keycloak-password.py',
                '--population','/root/enmaas-test-users','--index','1','--stdout')},
        'bootstrap_administrator_key':instruction('gateway','cat','/root/pricetag-admin/admin.key'),
        'mock_benchmark':instruction('gateway','python3','tests/external-services/benchmark.py','--detach','--duration','600','--offered-rps','100'),
        'provider_inventory':instruction('gateway','python3','scripts/pricetag/providers','list'),
        'provider_replacement':{
            'before_running':'Stop the benchmark, verify cleanup and save its report first. Run a replacement command in your private terminal; credentials are prompted without echo. Review prices and refresh clients afterward. Historical mock usage remains.',
            'openai':instruction('gateway','python3','scripts/pricetag/providers','openai','--replace',interactive=True),
            'external':instruction('gateway','python3','scripts/pricetag/providers','external','--api','both','--replace','--refresh-models',interactive=True)},
    }


def remote(config,role,argv,timeout=60,*,in_repository=True):
    options=transfer.ssh_options(config['ssh_key'],config['known_hosts'])
    command=shlex.join(['sudo','-n',*argv])
    if in_repository:command='cd '+shlex.quote(config['repository'])+' && '+command
    try:
        result=subprocess.run([*options,config['hosts'][role]['ssh'],command],stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=timeout)
        if result.returncode:raise ValueError('Remote command failed')
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise ValueError('Remote operation failed; inspect the selected host. Remote output is suppressed to protect credentials.') from None


CHECKOUT='''import os,stat,subprocess,sys
from pathlib import Path
repository,source,revision=sys.argv[1:]
if os.geteuid()!=0:sys.exit(2)
parent=Path(repository).parent
# /opt and the common source parent must not be symlinks or writable by others.
for path in (parent.parent,parent):
 if not path.exists():path.mkdir(mode=0o755)
 info=path.lstat()
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:sys.exit(2)
os.mkdir(repository,0o755)
env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0')
git=['git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
     '-c','credential.helper=','-c','protocol.file.allow=never','-C',repository]
def run(*args):
 return subprocess.run([*git,*args],env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=1800).stdout
run('init','--template=')
run('remote','add','origin',source)
run('fetch','--no-tags','--no-recurse-submodules','--depth=1','origin',revision)
if run('rev-parse','FETCH_HEAD').decode().strip()!=revision:sys.exit(2)
run('checkout','--detach','FETCH_HEAD')
if run('rev-parse','HEAD').decode().strip()!=revision:sys.exit(2)
if run('status','--porcelain','--untracked-files=all').strip():sys.exit(2)
'''

PREFLIGHT='''import os,platform,stat,subprocess,sys
from pathlib import Path
repository,revision,fresh,source_ready=sys.argv[1:]
if os.geteuid()!=0:sys.exit(2)
if platform.machine() not in ('x86_64','aarch64'):sys.exit(2)
release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if release.get('ID','').strip('"')!='rhel':sys.exit(2)
if source_ready=='yes':
 info=os.lstat(repository)
 if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:sys.exit(2)
 git=['git','-c','safe.directory='+repository,'-c','core.fsmonitor=false','-C',repository]
 if subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip()!=revision:sys.exit(2)
 if subprocess.check_output([*git,'status','--porcelain','--untracked-files=all'],text=True).strip():sys.exit(2)
elif os.path.lexists(repository):sys.exit(2)
if fresh=='yes':
 for name in ('/etc/praxis-pricetag','/etc/enmaas-keycloak-lab','/etc/enmaas-observability-lab',
              '/root/enmaas-tls','/root/enmaas-inputs','/root/enmaas-keycloak-inputs',
              '/root/enmaas-observability-inputs','/root/enmaas-telemetry-inputs',
              '/root/enmaas-test-users','/root/enmaas-population-import','/root/enmaas-personas'):
  if os.path.lexists(name):sys.exit(2)
'''


def preflight(config,role,fresh,source_ready=False):
    remote(config,role,['python3','-c',PREFLIGHT,config['repository'],config['revision'],
                       'yes' if fresh else 'no','yes' if source_ready else 'no'],in_repository=False)


def execute(config,step):
    role=step['role']
    if step['kind']=='generate-tls':
        certificates.generate(Path(config['tls_directory']),
            {r:[config['hosts'][r]['ssh'].split('@')[1],config['hosts'][r]['private_ip']] for r in ROLES},
            dns_names={'keycloak':config['issuer_name']})
        return
    host=config['hosts'][role]['ssh']
    if step['kind']=='tls':
        transfer.transfer_bundle(host,role,Path(config['tls_directory'])/role,config['ssh_key'],config['known_hosts'])
    elif step['kind']=='client':
        transfer.transfer(config['hosts']['keycloak']['ssh'],host,step.get('client',role),config['ssh_key'],config['known_hosts'])
    else:remote(config,role,step['argv'],timeout=5400,in_repository=not step.get('outside_repository',False))


def verify_source(config):
    """Do not mix an uncommitted coordinator with a different remote revision."""
    git=['git','-c','core.fsmonitor=false','-C',str(HERE.parents[1])]
    try:
        revision=subprocess.run([*git,'rev-parse','HEAD'],capture_output=True,text=True,timeout=15)
        clean=subprocess.run([*git,'status','--porcelain','--untracked-files=all'],capture_output=True,text=True,timeout=15)
        if revision.returncode or clean.returncode or revision.stdout.strip()!=config['revision'] or clean.stdout.strip():
            raise ValueError('Source mismatch')
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise ValueError('Commit and review the coordinator checkout, select its exact revision, and publish that commit before applying.') from None


def verify_issuer_resolution(config):
    """Check the operator/browser resolver without changing system DNS.

    This is not authoritative DNS verification or proof of other clients' views.
    Keep TLS hostname verification and the exact private AddHost mapping enabled.
    """
    validate(config)
    program = "import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],8443,type=socket.SOCK_STREAM)})))"
    try:
        result = subprocess.run([sys.executable, '-I', '-c', program, config['issuer_name']],
                                capture_output=True, text=True, timeout=10)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError('Resolver failed')
        addresses = json.loads(result.stdout)
        if addresses != [config['hosts']['keycloak']['ssh'].split('@')[1]]:
            raise ValueError('Issuer resolves outside the selected identity host')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValueError('Issuer DNS must resolve only to the selected Keycloak public IPv4 on this workstation. Check DNS/hosts configuration before installation.') from None


def apply(config,root,*,confirm):
    steps=plan(config)
    if confirm!='install '+config['deployment']:raise ValueError('Exact deployment confirmation required')
    verify_source(config)
    verify_issuer_resolution(config)
    binding={'sha256':hashlib.sha256(json.dumps({'config':config,'steps':steps},sort_keys=True).encode()).hexdigest()}
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
        state.save(root,'manifest.json',binding,exclusive=True)
    directory=state.secure_dir(root)
    try:lock=os.open('.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
    finally:os.close(directory)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if state.load(root,'manifest.json')!=binding:raise ValueError('Service state belongs to a different configuration or plan')
        completed=set();gap=False
        for step in steps:
            try:receipt=state.load(root,step['id']+'.json')
            except FileNotFoundError:
                gap=True;continue
            if gap:raise ValueError('Service receipts are out of order; reconcile before continuing')
            if receipt!={'state':'completed','step':step['id']}:
                raise ValueError('Uncertain service stage; inspect host and receipts before reconciliation')
            completed.add(step['id'])
        # Local TLS/package/checkout receipts do not establish that a host is
        # initialized. Keep refusing pre-existing deployment state until that
        # host's own TLS transfer is recorded complete.
        for role in ROLES:
            preflight(config,role,fresh=role+'-tls' not in completed,
                      source_ready=role+'-checkout' in completed)
        for index,step in enumerate(steps,1):
            if step['id'] in completed:continue
            state.save(root,step['id']+'.json',{'state':'started','step':step['id']},exclusive=True)
            print(f"[{index}/{len(steps)}] {step['id']}",flush=True)
            try:execute(config,step)
            except Exception:
                raise ValueError('Service stage failed or result uncertain: '+step['id']+'. Preserve receipts; no automatic retry or rollback.') from None
            state.save(root,step['id']+'.json',{'state':'completed','step':step['id']})
        return {'deployment':config['deployment'],'completed_steps':len(steps),'acceptance_verified':False,
                'issuer_users':config['population_count'],'pricetag_identities_imported':config['population_count'],
                'population_access_configured':True,'dashboard_personas_configured':True,'benchmark_run':False,
                'access':access_summary(config)}
    finally:os.close(lock)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--state',type=Path)
    parser.add_argument('--access',action='store_true',help='show configured URLs and safe SSH command instructions; no connections or credentials read')
    parser.add_argument('--confirm',help='install DEPLOYMENT_ID; omit for an offline preview')
    args=parser.parse_args();config=state.load(args.config.parent,args.config.name)
    if args.access:
        if args.confirm or args.state is not None:raise ValueError('--access does not install or inspect receipts')
        result=access_summary(config)
    elif args.confirm:
        if args.state is None:raise ValueError('Apply requires a private --state directory')
        result=apply(config,args.state,confirm=args.confirm)
    else:result={'deployment':validate(config)['deployment'],'steps':plan(config),'changed':False}
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):
        raise SystemExit('Service orchestration stopped. Inspect the selected hosts and private receipts; no credential diagnostics printed.') from None
