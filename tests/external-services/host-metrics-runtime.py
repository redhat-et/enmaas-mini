#!/usr/bin/env python3
"""Qualify the sampler's systemd sandbox only in the local Podman Linux VM."""
import importlib.util
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Executed only through `podman machine ssh`, never against a supplied SSH host.
REMOTE = r'''
import http.server,json,os,pwd,secrets,shutil,ssl,subprocess,sys,threading,time
from pathlib import Path
assert os.geteuid()==0
assert pwd.getpwnam('praxis-svc').pw_uid!=0
payload=json.load(sys.stdin)
name='enmaas-host-test-'+secrets.token_hex(8)
root=Path('/run')/name
root.mkdir(mode=0o755)
unit=Path('/run/systemd/system')/(name+'.service')
timer=unit.with_suffix('.timer')
received=[]
server=None

def ctl(*args,check=True):
    result=subprocess.run(['systemctl',*args],capture_output=True,text=True,timeout=25)
    if check and result.returncode:raise RuntimeError('Test systemd operation failed')
    return result

try:
    for filename,value in payload['files'].items():
        assert '/' not in filename
        path=root/filename
        path.write_text(value);path.chmod(0o644 if filename=='sampler.py' else 0o600)
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            length=int(self.headers.get('Content-Length','0'))
            if self.path!='/v1/metrics' or not 0<length<=65536:
                self.send_error(400);return
            data=json.loads(self.rfile.read(length))
            metrics=data['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
            assert {m['name'] for m in metrics}=={'enmaas_host_cpu_seconds','enmaas_host_memory_bytes','enmaas_host_load','enmaas_host_disk_bytes'}
            received.append(time.monotonic())
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{}')
    server=http.server.ThreadingHTTPServer((payload['address'],24443),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root/'server.pem',root/'server.key')
    context.load_verify_locations(root/'telemetry-ca.pem');context.verify_mode=ssl.CERT_REQUIRED
    server.socket=context.wrap_socket(server.socket,server_side=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    service=payload['service'].replace('/usr/local/libexec/enmaas-host-metrics',str(root/'sampler.py')).replace('/etc/enmaas-telemetry-lab',str(root))
    unit.write_text(service);timer.write_text(payload['timer'])
    ctl('daemon-reload');ctl('start',timer.name)
    deadline=time.monotonic()+40
    while len(received)<2 and time.monotonic()<deadline:time.sleep(0.5)
    assert len(received)>=2,'Timer did not deliver two samples'
    ctl('stop',timer.name)
    ctl('stop',unit.name)
    before=len(received)
    unit.write_text(service.replace('IPAddressAllow='+payload['address']+'/32','IPAddressAllow=192.0.2.1/32'))
    ctl('daemon-reload')
    denied=ctl('start',unit.name,check=False)
    assert denied.returncode!=0 and len(received)==before,'IP sandbox allowed the excluded receiver'
    print(json.dumps({'timer_samples':before,'mtls_delivery':True,'service_user':'praxis-svc','ip_deny_enforced':True,'rhel_qualified':False}))
finally:
    try:
        ctl('stop',timer.name,unit.name,check=False)
    finally:
        unit.unlink(missing_ok=True);timer.unlink(missing_ok=True)
        ctl('daemon-reload');ctl('reset-failed',unit.name,check=False)
        if server:server.shutdown();server.server_close()
        shutil.rmtree(root)
'''


def main():
    result = subprocess.run(['podman', 'machine', 'ssh', 'ip -j -4 addr show scope global'],
                            capture_output=True, text=True, check=True, timeout=15)
    addresses = [entry['local'] for link in json.loads(result.stdout)
                 for entry in link.get('addr_info', []) if entry.get('family') == 'inet']
    address = next((value for value in addresses if any(ipaddress.IPv4Address(value) in ipaddress.IPv4Network(block)
                   for block in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))), None)
    if not address:
        raise RuntimeError('Local VM has no private IPv4 address')
    with tempfile.TemporaryDirectory(prefix='enmaas-host-unit-') as temporary:
        root = Path(temporary)
        load('certificates').generate(root/'tls', {role: [address] for role in ('gateway', 'keycloak', 'observability')}, days=1)
        config = root/'config'; config.mkdir()
        load('telemetry-agent').configure(config, address)
        paths = {'server.pem': 'observability/server.pem', 'server.key': 'observability/server.key',
                 'telemetry-ca.pem': 'trust/telemetry-ca.pem', 'server-ca.pem': 'trust/issuer-ca.pem',
                 'client.pem': 'gateway/telemetry-client.pem', 'client.key': 'gateway/telemetry-client.key'}
        files = {name: (root/'tls'/path).read_text() for name, path in paths.items()}
        files['sampler.py'] = (HERE/'host-metrics.py').read_text()
        payload = {'address': address, 'files': files,
                   'service': (config/'systemd/enmaas-host-metrics.service').read_text(),
                   'timer': (config/'systemd/enmaas-host-metrics.timer').read_text()}
        result = subprocess.run(['podman', 'machine', 'ssh', shlex.join(['sudo', 'python3', '-c', REMOTE])],
                                input=json.dumps(payload), capture_output=True, text=True, timeout=100)
        if result.returncode:
            raise RuntimeError('Local VM systemd qualification failed; inspect enmaas-host-test units')
        print(result.stdout.strip())


if __name__ == '__main__':
    main()
