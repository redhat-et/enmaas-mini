#!/usr/bin/env python3
"""Send aggregate Linux host measurements to the lab collector with mTLS."""
import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request


def endpoint(address):
    address=ipaddress.IPv4Address(address)
    if not any(address in ipaddress.IPv4Network(block) for block in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
        raise ValueError('Use the observation private IPv4 address')
    return f'https://{address}:24443/v1/metrics'


def read(path):
    with path.open() as stream:text=stream.read(1024*1024+1)
    if len(text)>1024*1024:raise ValueError('Kernel input exceeded bound')
    return text


def sample(proc=Path('/proc'),now_ns=None):
    now_ns=now_ns or time.time_ns()
    stat={line.split()[0]:line.split()[1:] for line in read(proc/'stat').splitlines() if line.split()}
    cpu=[int(value) for value in stat['cpu'][:8]]
    boot=int(stat['btime'][0]);ticks=os.sysconf('SC_CLK_TCK')
    memory={line.split(':')[0]:int(line.split()[1])*1024 for line in read(proc/'meminfo').splitlines() if ':' in line}
    total,available,cached=(memory[key] for key in ('MemTotal','MemAvailable','Cached'))
    loads=[float(value) for value in read(proc/'loadavg').split()[:3]]
    if (len(cpu)!=8 or min(cpu)<0 or boot<=0 or boot*10**9>now_ns or ticks<=0
            or not 0<=available<=total or not 0<=cached<=total or total<=0
            or len(loads)!=3 or any(not math.isfinite(value) or value<0 for value in loads)):
        raise ValueError('Invalid aggregate kernel measurements')
    disk=os.statvfs('/var')
    if not 0<=disk.f_bavail<=disk.f_bfree<=disk.f_blocks or disk.f_frsize<=0:
        raise ValueError('Invalid filesystem capacity')
    def points(values,counter=False):
        result=[]
        for state,value in values:
            point={'timeUnixNano':str(now_ns),'asDouble':value,
                   'attributes':[{'key':'state','value':{'stringValue':state}}]}
            if counter:point['startTimeUnixNano']=str(boot*10**9)
            result.append(point)
        return result
    metrics=[{'name':'enmaas_host_cpu_seconds','sum':{'aggregationTemporality':2,'isMonotonic':True,
              'dataPoints':points(zip(('user','nice','system','idle','iowait','irq','softirq','steal'),[n/ticks for n in cpu]),True)}},
             {'name':'enmaas_host_memory_bytes','gauge':{'dataPoints':points([
                 ('total',total),('available',available),('unavailable',total-available),('cached',cached)])}},
             {'name':'enmaas_host_load','gauge':{'dataPoints':points(zip(('1m','5m','15m'),loads))}},
             {'name':'enmaas_host_disk_bytes','gauge':{'dataPoints':points([
                 ('total',disk.f_blocks*disk.f_frsize),('available',disk.f_bavail*disk.f_frsize),
                 ('used',(disk.f_blocks-disk.f_bfree)*disk.f_frsize)])}}]
    return {'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-gateway-host'}}]},
             'scopeMetrics':[{'scope':{'name':'enmaas-host'},'metrics':metrics}]}]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def send(address,credentials,payload):
    context=ssl.create_default_context(cafile=str(credentials/'ca.pem'))
    context.load_cert_chain(credentials/'client.pem',credentials/'client.key')
    request=urllib.request.Request(endpoint(address),data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())
    with opener.open(request,timeout=5) as response:
        result=json.loads(response.read(65537))
        if response.status!=200 or result.get('partialSuccess'):
            raise ValueError('Collector rejected host measurements')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observability-private-ip',required=True)
    args=parser.parse_args()
    endpoint(args.observability_private_ip)
    credentials=Path(os.environ['CREDENTIALS_DIRECTORY'])
    send(args.observability_private_ip,credentials,sample())
    print('Aggregate host measurements delivered')

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,urllib.error.URLError):
        raise SystemExit('Host metrics failed; inspect credentials, collector health and private networking') from None
