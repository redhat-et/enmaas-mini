#!/usr/bin/env python3
"""Prepare a private gateway metrics/trace relay to the remote mTLS collector."""
import argparse
import importlib.util
import ipaddress
from pathlib import Path
import yaml

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('observation',HERE/'observability-host.py')
observation=importlib.util.module_from_spec(spec);spec.loader.exec_module(observation)
lab=observation.lab


def configuration(address):
    address=ipaddress.IPv4Address(address)
    if not any(address in ipaddress.IPv4Network(block) for block in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
        raise ValueError('Use the observability host private IPv4 address')
    config = {
        'receivers':{
            'otlp':{'protocols':{'grpc':{'endpoint':'127.0.0.1:4317'}}},
            'prometheus':{'config':{'scrape_configs':[
                {'job_name':name,'scrape_interval':'15s','scrape_timeout':'5s','sample_limit':10000,
                 'static_configs':[{'targets':[target]}]}
                for name,target in [('praxis','127.0.0.1:9901'),('pricetag-metering','pricetag-metering:9902')]]}}},
        'processors':{
            'memory_limiter':{'check_interval':'1s','limit_mib':192,'spike_limit_mib':32},
            'transform':observation.privacy_transform(),
            'batch':{'timeout':'5s','send_batch_size':256,'send_batch_max_size':512}},
        'exporters':{'otlphttp/remote':{
            'endpoint':f'https://{address}:24443',
            'tls':{'ca_file':'/lab/server-ca.pem','cert_file':'/lab/client.pem','key_file':'/lab/client.key'},
            'timeout':'10s','sending_queue':{'enabled':True,'queue_size':256,'num_consumers':2},
            'retry_on_failure':{'enabled':True,'max_elapsed_time':'30s'}}},
        'service':{'pipelines':{
            'metrics':{'receivers':['prometheus'],'processors':['memory_limiter','transform','batch'],'exporters':['otlphttp/remote']},
            'traces':{'receivers':['otlp'],'processors':['memory_limiter','transform','batch'],'exporters':['otlphttp/remote']}}}}

    names=('pricetag-gateway','pricetag-metering','pricetag-maas')
    for name in names:
        config['receivers']['filelog/'+name]={
            'include':['/logs/'+name+'.log'],'start_at':'end',
            'include_file_path':False,'include_file_name':False,
            'max_log_size':'64KiB','storage':'file_storage/logs',
            'resource':{'service.name':name},
            'operators':[{'type':'container','id':'container-parser'}]}
    config['extensions']={'file_storage/logs':{'directory':'/positions'}}
    config['service']['extensions']=['file_storage/logs']
    config['service']['pipelines']['logs']={
        'receivers':['filelog/'+name for name in names],
        'processors':['memory_limiter','transform','batch'],'exporters':['otlphttp/remote']}
    return config


def configure(root,address):
    config=configuration(address)
    if any(root.iterdir()):raise FileExistsError('Use a new empty private output directory')
    root.chmod(0o700)
    lab.write(root,'collector.yaml',yaml.safe_dump(config,sort_keys=False))
    units=root/'quadlets';units.mkdir(mode=0o700)
    lab.write(units,'lab-gateway-telemetry.container',f'''[Unit]
Requires=pricetag-gateway.service
After=pricetag-gateway.service
BindsTo=pricetag-gateway.service
PartOf=pricetag-gateway.service
[Container]
Image={lab.IMAGES['collector']}
ContainerName=lab-gateway-telemetry
User=0:0
Volume=%h/.local/share/enmaas-telemetry-logs:/logs:ro,z
Volume=lab-gateway-log-positions.volume:/positions:U
Network=container:pricetag-gateway
NoNewPrivileges=true
DropCapability=all
ReadOnly=true
PidsLimit=128
Secret=lab-gateway-telemetry-config,target=/lab/collector.yaml
Secret=lab-gateway-telemetry-ca,target=/lab/server-ca.pem
Secret=lab-gateway-telemetry-cert,target=/lab/client.pem
Secret=lab-gateway-telemetry-key,target=/lab/client.key
Exec=--config=/lab/collector.yaml
[Service]
MemoryMax=256M
Restart=on-failure
TimeoutStartSec=120
[Install]
WantedBy=default.target
''')
    lab.write(units,'lab-gateway-log-positions.volume','[Volume]\nVolumeName=enmaas-lab-gateway-log-positions\n')
    services=root/'systemd';services.mkdir(mode=0o700)
    lab.write(services,'enmaas-host-metrics.service',f'''[Unit]
Description=Aggregate gateway host telemetry for the external-services lab
After=network-online.target
[Service]
Type=oneshot
User=praxis-svc
Group=praxis-svc
ExecStart=/usr/bin/python3 /usr/local/libexec/enmaas-host-metrics --observability-private-ip {address}
LoadCredential=ca.pem:/etc/enmaas-telemetry-lab/server-ca.pem
LoadCredential=client.pem:/etc/enmaas-telemetry-lab/client.pem
LoadCredential=client.key:/etc/enmaas-telemetry-lab/client.key
NoNewPrivileges=yes
CapabilityBoundingSet=
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_UNIX
IPAddressDeny=any
IPAddressAllow={address}/32
MemoryMax=96M
TasksMax=16
TimeoutStartSec=12
''')
    lab.write(services,'enmaas-host-metrics.timer','''[Unit]
Description=Sample aggregate gateway host metrics every 15 seconds
[Timer]
OnBootSec=30s
OnUnitActiveSec=15s
AccuracySec=1s
[Install]
WantedBy=timers.target
''')
    for name in ('pricetag-gateway','pricetag-metering','pricetag-maas'):
        lab.write(root,name+'-logs.conf',
            '[Container]\nLogDriver=k8s-file\nLogOpt=path=%h/.local/share/enmaas-telemetry-logs/'+name+'.log\nLogOpt=max-size=10mb\n')
    # Exporter environment survives provider config regeneration. Installation
    # must add this drop-in, then restart the gateway and relay together.
    lab.write(root,'gateway-telemetry.conf','[Unit]\nWants=lab-gateway-telemetry.service\n[Container]\nEnvironment=OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317\nEnvironment=OTEL_SERVICE_NAME=enmaas-gateway\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observability-private-ip',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    configure(args.output,args.observability_private_ip)
    print('Gateway relay inputs prepared; installation and runtime qualification remain separate.')

if __name__=='__main__':main()
