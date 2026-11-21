"""Synthetic query-contract samples, never production or benchmark observations."""


def series():
    """Return (name, labels, initial value, per-second increase) for each series.

    The fixture tests query/label transport. It does not pretend to measure the
    test host or replace the application/exporter integration acceptance tests.
    """
    values=[]
    def add(name,value,rate=0,**labels):values.append((name,labels,value,rate))
    for host in ('host-1','host-2'):
        add('enmaas_host_sample_unixtime_seconds',0,1,host=host)
        for state,rate in (('user',1.2),('idle',2.8)):
            add('enmaas_host_cpu_seconds_total',1000,rate,host=host,state=state)
        for state,value in (('total',8*1024**3),('available',6*1024**3),('unavailable',2*1024**3),('cached',1024**3)):
            add('enmaas_host_memory_bytes',value,host=host,state=state)
        for state,value in (('total',100*1024**3),('available',70*1024**3)):
            add('enmaas_host_disk_bytes',value,host=host,state=state)
        for state,value in (('1m',1),('5m',0.8),('15m',0.6)):
            add('enmaas_host_load',value,host=host,state=state)
        for component in ('gateway-sso','metering-sso','maas','db'):
            labels=dict(host=host,component=component,replica='primary')
            add('enmaas_container_cpu_seconds_total',100,0.5,**labels)
            add('enmaas_container_memory_bytes',128*1024**2,**labels)
            add('enmaas_container_memory_limit_bytes',512*1024**2,**labels)
            add('enmaas_container_pids',10,**labels)
    for name,value,rate in (
        ('connections',10,0),('active_connections',2,0),('lock_waiters',0,0),
        ('connection_limit',100,0),('transactions_committed_total',1000,10),
        ('transactions_rolled_back_total',0,0),('deadlocks_total',0,0),
        ('temp_bytes_total',1024,100),('size_bytes',1024**3,0)):
        add('enmaas_postgres_'+name,value,rate,host='database-1',component='db')
    for name,value in (('open',10),('in_use',3),('idle',7)):
        add('metering_db_pool_'+name+'_conns',value)
    for name,rate in (('db_pool_wait_seconds_total',0.01),('events_ingested_total',10),
                      ('usage_event_insert_errors_total',0),('quota_checks_total',10),('quota_denials_total',0)):
        add('metering_'+name,100 if rate else 0,rate)
    # Buckets use known cumulative counts, so p95 is finite and independently
    # testable. Gauge-encoded fixture series preserve exact Prometheus names;
    # the production OTLP counter/histogram encoders have separate tests.
    for bound,rate in (('0.01',5),('0.1',10),('+Inf',10)):
        add('metering_usage_event_insert_duration_seconds_bucket',100,rate,le=bound)
    add('praxis_process_open_fds',20);add('praxis_process_max_fds',1000)
    add('praxis_http_active_requests',4,listener='https')
    for cluster in ('authenticated-openai','authenticated-anthropic'):
        for status,rate in (('2xx',10),('3xx',0),('4xx',0),('5xx',0)):
            add('praxis_http_requests_total',100 if rate else 0,rate,cluster=cluster,status_class=status)
            add('praxis_upstream_requests_total',100 if rate else 0,rate,cluster=cluster,status_class=status)
        add('praxis_http_request_duration_seconds_sum',1000,20,cluster=cluster)
        add('praxis_http_request_duration_seconds_count',500,10,cluster=cluster)
    add('praxis_rate_limit_limited_total',0)
    return values
