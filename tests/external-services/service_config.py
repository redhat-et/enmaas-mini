"""Shared protected observability configuration; no deployment or benchmark imports."""
import base64
import importlib.util
import json
from pathlib import Path
import secrets
import yaml

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('service_realm_inputs', HERE/'realm.py')
realm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(realm)
spec = importlib.util.spec_from_file_location('service_monitoring', HERE/'monitoring.py')
monitoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitoring)
write = realm.write
IMAGES = realm.IMAGES
PERSES_ASSETS = ('project.json', 'datasource.json', 'dashboard.json',
                 'tempo-datasource.json', 'loki-datasource.json', *monitoring.all_dashboards())


def privacy_transform(*, classify=False):
    resource=['keep_keys(attributes, ["service.name", "service.version", "deployment.environment.name"])']
    scope=['set(name, "enmaas")','set(version, "")','keep_keys(attributes, [])']
    attributes='keep_keys(attributes, ["http.request.method", "http.response.status_code", "error.type", "gen_ai.provider.name", "gen_ai.request.model"])'
    inference='attributes["http.request.method"] == "POST" and (attributes["url.path"] == "/v1/chat/completions" or attributes["url.path"] == "/v1/responses" or attributes["url.path"] == "/v1/messages")'
    trace_attributes=attributes.replace('"http.request.method",', '"enmaas.request.kind", "http.request.method",')
    reset='set(attributes["enmaas.request.kind"], "gateway")'
    if not classify: reset += ' where attributes["enmaas.request.kind"] != "inference"'
    transform={'error_mode':'propagate',
        'log_statements':[{'context':'resource','statements':resource},{'context':'scope','statements':scope},
            {'context':'log','statements':[attributes,'set(body, "gateway event")','set(severity_text, "")']}],
        'trace_statements':[{'context':'resource','statements':resource},{'context':'scope','statements':scope},
            {'context':'span','statements':[reset,'set(attributes["enmaas.request.kind"], "inference") where '+inference,trace_attributes,'set(name, "request")','set(name, "inference") where attributes["enmaas.request.kind"] == "inference"','set(status.message, "")','set(trace_state, "")','set(links, nil)']},
            {'context':'spanevent','statements':['set(name, "event")','keep_keys(attributes, [])']}],
        'metric_statements':[{'context':'resource','statements':resource},{'context':'scope','statements':scope},
            {'context':'metric','statements':['set(description, "")','set(unit, "")']},
            {'context':'datapoint','statements':['keep_keys(attributes, ["http.request.method", "http.response.status_code", "gen_ai.request.model", "state", "component", "host", "replica", "cpu", "device", "direction", "type", "cluster", "route", "listener", "endpoint", "method", "status_class", "filter", "phase", "quantile", "le", "result", "reason"])','set(exemplars, nil) where metric.type != METRIC_DATA_TYPE_SUMMARY']}]}
    return transform


def transport_dashboard():
    return {
        'kind': 'Dashboard', 'metadata': {'name': 'local-transport', 'project': 'praxis'},
        'spec': {'display': {'name': 'Telemetry transport'}, 'duration': '15m',
            'panels': {'0': {'kind': 'Panel', 'spec': {
                'display': {'name': 'Test telemetry received',
                    'description': 'Synthetic transport qualification, not gateway usage. Empty until the transport probe sends a sample.'},
                'plugin': {'kind': 'TimeSeriesChart', 'spec': {
                    'legend': {'mode': 'list', 'position': 'bottom'},
                    'visual': {'areaOpacity': 0, 'lineWidth': 2}}},
                'queries': [{'kind': 'TimeSeriesQuery', 'spec': {
                    'plugin': {'kind': 'PrometheusTimeSeriesQuery', 'spec': {
                        'query': 'enmaas_lab_requests_total'}}}}]}}},
            'layouts': [{'kind': 'Grid', 'spec': {'items': [
                {'x': 0, 'y': 0, 'width': 24, 'height': 10, 'content': {'$ref': '#/spec/panels/0'}}]}}]}}


def configure(root, client_secret):
    if (not isinstance(client_secret, str) or not 16 <= len(client_secret) <= 4096
            or any(not 33 <= ord(c) <= 126 or c in '\\"' for c in client_secret)):
        raise ValueError('invalid confidential-client secret')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    if (root/'oauth.cfg').exists():
        raise FileExistsError('existing observability configuration')
    credentials = {'oidc': client_secret}
    write(root, 'oauth.cfg', f'''provider = "keycloak-oidc"
oidc_issuer_url = "https://localhost:18443/realms/enmaas-lab"
client_id = "observability"
client_secret = "{credentials['oidc']}"
redirect_url = "https://localhost:19443/oauth2/callback"
provider_ca_files = ["/lab/ca.pem"]
allowed_groups = ["/observability-admin"]
email_domains = ["example.test"]
scope = "openid profile email"
code_challenge_method = "S256"
https_address = "0.0.0.0:19443"
http_address = ""
tls_cert_file = "/lab/server.pem"
tls_key_file = "/lab/server.key"
upstreams = ["http://127.0.0.1:8080/"]
cookie_secret = "{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}"
cookie_secure = true
cookie_httponly = true
cookie_samesite = "lax"
cookie_expire = "5m"
cookie_refresh = "1m"
reverse_proxy = false
skip_provider_button = true
request_logging = false
auth_logging = false
''')
    write(root, 'perses.yaml', f'''database:
  file:
    folder: /perses
    extension: json
security:
  enable_auth: false
  encryption_key: {secrets.token_urlsafe(24)}
datasource:
  disable_local: true
  proxy:
    allowed_hosts: ['127.0.0.1']
    allowed_networks: ['127.0.0.1/32']
    http:
      max_conns_per_host: 16
      max_timeout: 10s
frontend:
  explorer:
    enable: true
provisioning:
  folders: [/etc/perses/lab]
''')
    write(root, 'project.json', json.loads((HERE/'perses/project.json').read_text()))
    write(root, 'datasource.json', {
        'kind': 'GlobalDatasource', 'metadata': {'name': 'prometheus'},
        'spec': {'default': True, 'plugin': {'kind': 'PrometheusDatasource',
            'spec': {'proxy': {'kind': 'HTTPProxy', 'spec': {'url': 'http://127.0.0.1:9090'}}}}}})
    write(root, 'dashboard.json', transport_dashboard())
    for name in ('tempo-datasource.json', 'loki-datasource.json'):
        write(root, name, json.loads((HERE/'perses'/name).read_text()))
    for name, document in monitoring.all_dashboards().items():
        write(root, name, document)
    write(root, 'monitoring-rules.yaml', yaml.safe_dump(monitoring.rules(), sort_keys=False))
    write(root, 'prometheus.yaml', '''rule_files:
  - /lab/monitoring-rules.yaml
global:
  evaluation_interval: 15s
  scrape_interval: 5s
scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['127.0.0.1:9090']
  - job_name: collector
    static_configs:
      - targets: ['127.0.0.1:8889']
''')
    write(root, 'loki.yaml', '''auth_enabled: false
server:
  http_listen_port: 3100
common:
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
storage_config:
  filesystem:
    directory: /loki/chunks
limits_config:
  retention_period: 24h
compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
''')
    write(root, 'tempo.yaml', '''server:
  http_listen_port: 3200
  grpc_listen_port: 9096
distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 127.0.0.1:4319
storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
backend_scheduler:
  provider:
    compaction:
      compaction:
        block_retention: 24h
usage_report:
  reporting_enabled: false
''')
    write(root, 'collector.yaml', '''receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:24443
        max_request_body_size: 4194304
        tls:
          cert_file: /lab/server.pem
          key_file: /lab/server.key
          client_ca_file: /lab/ca.pem
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 128
    spike_limit_mib: 32
  batch:
    timeout: 5s
    send_batch_size: 256
    send_batch_max_size: 512
exporters:
  prometheus:
    endpoint: 127.0.0.1:8889
    send_timestamps: true
  otlphttp/loki:
    endpoint: http://127.0.0.1:3100/otlp
  otlphttp/tempo:
    endpoint: http://127.0.0.1:4319
service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheus]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, transform, batch]
      exporters: [otlphttp/loki]
    traces:
      receivers: [otlp]
      processors: [memory_limiter, transform, batch]
      exporters: [otlphttp/tempo]
''')
    collector = yaml.safe_load((root/'collector.yaml').read_text())
    collector['processors']['transform'] = privacy_transform()
    for pipeline in collector['service']['pipelines'].values():
        pipeline['processors'] = ['memory_limiter', 'transform', 'batch']
    write(root, 'collector.yaml', yaml.safe_dump(collector, sort_keys=False))
