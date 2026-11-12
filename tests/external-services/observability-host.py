#!/usr/bin/env python3
"""Prepare rootless observation-host inputs; credentials stay in private files."""
import argparse
import importlib.util
import ipaddress
from pathlib import Path
import stat
import tempfile
import yaml

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('local_lab',HERE/'local.py')
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
spec=importlib.util.spec_from_file_location('issuer_identity',HERE/'issuer-config.py')
identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity)


# Shared policy for the local receiver, RHEL receiver and gateway agent.
privacy_transform = lab.privacy_transform

def private_address(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in ipaddress.IPv4Network(network) for network in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('collector binding requires an explicit RFC1918 IPv4 address')
    return str(address)


def validate_host_binding(root, interfaces):
    bindings = [line.split('=', 1)[1] for line in
                (root/'quadlets/lab-observability.pod').read_text().splitlines()
                if line.startswith('PublishPort=')]
    private = [line[:-len(':24443:24443')] for line in bindings
               if line.endswith(':24443:24443')]
    if len(private) != 1 or sorted(bindings) != sorted(['8443:8443', private[0]+':24443:24443']):
        raise ValueError('unexpected public/collector port bindings')
    address = private_address(private[0])
    assigned = {entry.get('local') for interface in interfaces
                for entry in interface.get('addr_info', []) if entry.get('family') == 'inet'}
    if address not in assigned:
        raise ValueError('collector private address is not assigned to this host')
    for unit in (root/'quadlets').glob('*.container'):
        if any(line.startswith('PublishPort=') for line in unit.read_text().splitlines()):
            raise ValueError('service ports must be published only by the reviewed pod')
    return address


def configure(root,issuer,observability,client_secret,private_ip,*,issuer_name=None,issuer_private_ip=None):
    private_ip = private_address(private_ip)
    issuer,observability=(str(ipaddress.IPv4Address(value)) for value in (issuer,observability))
    issuer,mapping=identity.resolve(issuer,issuer_name,issuer_private_ip)
    if not 16<=len(client_secret)<=4096 or any(not 33<=ord(c)<=126 or c in '\\"' for c in client_secret):
        raise ValueError('invalid confidential-client secret; use visible ASCII without quotes/backslashes')
    if any(root.iterdir()):raise FileExistsError('use a new empty output directory')
    root.chmod(0o700)
    names=('oauth.cfg','perses.yaml','prometheus.yaml','loki.yaml','tempo.yaml','collector.yaml',
           *lab.PERSES_ASSETS)
    with tempfile.TemporaryDirectory() as temporary:
        source=Path(temporary);lab.configure(source,certificates=False)
        for name in names:lab.write(root,name,(source/name).read_text())
    auth=(root/'oauth.cfg').read_text()
    lines=[]
    for line in auth.splitlines():
        if line.startswith('client_secret ='):line='client_secret = "'+client_secret+'"'
        elif line.startswith('email_domains ='):line='email_domains = ["*"]' # explicit group gate remains mandatory
        lines.append(line)
    auth='\n'.join(lines)+'\n'
    auth=auth.replace('https://localhost:18443',f'https://{issuer}:8443')
    auth=auth.replace('https://localhost:19443',f'https://{observability}:8443')
    auth=auth.replace('0.0.0.0:19443','0.0.0.0:8443').replace('/lab/ca.pem','/lab/issuer-ca.pem')
    lab.write(root,'oauth.cfg',auth)
    collector=yaml.safe_load((root/'collector.yaml').read_text())
    collector['receivers']['otlp']['protocols']['http']['tls']['client_ca_file']='/lab/telemetry-ca.pem'
    # Raw log text, URLs, request headers and user identifiers are never required
    # for this test-host dashboard. Keep only reviewed bounded label names.
    transform=privacy_transform()
    collector['processors']['transform']=transform
    collector['service']['pipelines']['metrics']['processors']=['memory_limiter','transform','batch']
    for name in ('otlphttp/loki','otlphttp/tempo'):
        collector['exporters'][name]['sending_queue']={'enabled':True,'queue_size':256,'num_consumers':2}
        collector['exporters'][name]['retry_on_failure']={'enabled':True,'max_elapsed_time':'30s'}
    lab.write(root,'collector.yaml',yaml.safe_dump(collector,sort_keys=False))
    units=root/'quadlets';units.mkdir(mode=0o700)
    host_mapping = f'AddHost={mapping}\n' if mapping else ''
    lab.write(units,'lab-observability.pod',f'[Pod]\nPodName=lab-observability\n{host_mapping}PublishPort=8443:8443\nPublishPort={private_ip}:24443:24443\n[Install]\nWantedBy=default.target\n')
    components={
        'auth':('oauth.cfg','--config=/lab/oauth.cfg','256M',None),
        'perses':('perses.yaml','--config=/lab/perses.yaml','512M','/perses'),
        'prometheus':('prometheus.yaml','--config.file=/lab/prometheus.yaml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=48h --storage.tsdb.retention.size=512MB','1G','/prometheus'),
        'loki':('loki.yaml','-config.file=/lab/loki.yaml','1G','/loki'),
        'tempo':('tempo.yaml','-config.file=/lab/tempo.yaml','1G','/var/tempo'),
        'collector':('collector.yaml','--config=/lab/collector.yaml','512M',None)}
    for name,(config,command,memory,volume) in components.items():
        body=f'[Container]\nImage={lab.IMAGES[name]}\nPod=lab-observability.pod\nContainerName=lab-obs-{name}\nNoNewPrivileges=true\nDropCapability=all\nPidsLimit=256\n'
        body+=f'Secret=lab-obs-{config.replace(".","-")},target=/lab/{config}\nExec={command}\n'
        if name in ('auth','collector'):
            body+='Secret=lab-obs-server-cert,target=/lab/server.pem\nSecret=lab-obs-server-key,target=/lab/server.key\n'
            ca='issuer' if name=='auth' else 'telemetry'
            body+=f'Secret=lab-obs-{ca}-ca,target=/lab/{ca}-ca.pem\n'
        if name=='perses':
            for asset in lab.PERSES_ASSETS:
                body+=f'Secret=lab-obs-{asset.replace(".","-")},target=/etc/perses/lab/{asset}\n'
        if volume:
            lab.write(units,f'lab-obs-{name}.volume',f'[Volume]\nVolumeName=enmaas-lab-obs-{name}\n')
            body+=f'Volume=lab-obs-{name}.volume:{volume}:U\n'
        body+=f'[Service]\nMemoryMax={memory}\nRestart=on-failure\nTimeoutStartSec=300\n[Install]\nWantedBy=default.target\n'
        lab.write(units,f'lab-obs-{name}.container',body)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issuer',required=True)
    parser.add_argument('--observability',required=True)
    parser.add_argument('--private-ip',required=True,help='private interface address for mTLS ingestion')
    parser.add_argument('--client-secret-file',type=Path,required=True)
    parser.add_argument('--issuer-name',help='canonical issuer DNS name; requires --issuer-private-ip')
    parser.add_argument('--issuer-private-ip',help='RFC1918 issuer address for server backchannels')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    info=args.client_secret_file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:
        parser.error('client secret must be a private regular file')
    with args.client_secret_file.open() as source:secret=source.read(4097).strip()
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    configure(args.output,args.issuer,args.observability,secret,args.private_ip,
              issuer_name=args.issuer_name,issuer_private_ip=args.issuer_private_ip)
    print('Private observability inputs prepared; no services started.')

if __name__=='__main__':main()
