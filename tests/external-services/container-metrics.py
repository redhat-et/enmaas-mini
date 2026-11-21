"""Bounded Podman component metrics; no command lines, container IDs or keys."""
import math
import re
import time

COMPONENTS=('gateway-sso','maas','metering-sso','provider','db','keycloak','auth','perses','prometheus','loki','tempo','collector','gateway-relay')


def size(text):
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|TB|KiB|MiB|GiB)',text.strip())
    if not match:raise ValueError('Invalid Podman byte quantity')
    value=float(match[1])*{'B':1,'kB':1000,'MB':1e6,'GB':1e9,'TB':1e12,'KiB':1024,'MiB':1024**2,'GiB':1024**3}[match[2]]
    if not math.isfinite(value) or value<0:raise ValueError('Invalid byte value')
    return value


def duration(text):
    parts=re.findall(r'([0-9]+(?:\.[0-9]+)?)(ns|µs|us|ms|s|m|h)',text)
    if not parts or ''.join(v+u for v,u in parts)!=text:raise ValueError('Invalid CPU time')
    return sum(float(v)*{'ns':1e-9,'µs':1e-6,'us':1e-6,'ms':1e-3,'s':1,'m':60,'h':3600}[u] for v,u in parts)


def inventory(names,prefix):
    """Select bounded service replicas from the already-owned pod inventory."""
    if not isinstance(names,list) or len(names)>512:raise ValueError('Invalid pod inventory')
    selected=[]
    for raw in names:
        if not isinstance(raw,str) or not raw.startswith(prefix+'-'):continue
        name,separator,replica=raw[len(prefix)+1:].partition('--')
        if name not in COMPONENTS:continue
        if separator and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}',replica):raise ValueError('Invalid replica alias')
        selected.append(raw)
    if not selected or len(selected)>256 or len(selected)!=len(set(selected)):raise ValueError('Invalid component inventory')
    return sorted(selected)


def payload(rows,prefix,now=None,host_id="gateway"):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}",host_id):raise ValueError("Invalid host alias")
    now=time.time_ns() if now is None else now
    metrics={name:[] for name in ('cpu_seconds','memory_bytes','memory_limit_bytes','pids')}
    seen=set()
    if not isinstance(rows,list) or len(rows)>256:raise ValueError('Invalid component inventory')
    for row in rows:
        raw=row['name']
        if not raw.startswith(prefix+'-'):raise ValueError('Foreign component')
        name,separator,replica=raw[len(prefix)+1:].partition('--')
        replica=replica if separator else 'primary'
        if name not in COMPONENTS or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}',replica) or raw in seen:raise ValueError('Foreign or duplicate component')
        seen.add(raw)
        used,limit=row['mem_usage'].split('/')
        values=(duration(row['cpu_time']),size(used),size(limit),int(row['pids']))
        if any(not math.isfinite(v) or v<0 for v in values) or (values[2]>0 and values[1]>values[2]):raise ValueError('Invalid container measurements')
        for key,value in zip(metrics,values):
            if key=='memory_limit_bytes' and value==0:continue
            metrics[key].append({'timeUnixNano':str(now),'asDouble':value,'attributes':[{'key':'component','value':{'stringValue':name}},{'key':'host','value':{'stringValue':host_id}},{'key':'replica','value':{'stringValue':replica}}]})
    document=[]
    for name,points in metrics.items():
        data={'dataPoints':points}
        counter=name=='cpu_seconds'
        if counter:data.update(aggregationTemporality=2,isMonotonic=True)
        document.append({'name':'enmaas_container_'+name, 'sum' if counter else 'gauge':data})
    return {'resourceMetrics':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'enmaas-container-sampler'}}]},'scopeMetrics':[{'metrics':document}]}]}
