# Dedicated observability test host

Deploy Perses, Prometheus, Loki, Tempo and an OpenTelemetry Collector on a
**fresh RHEL test VM**, with OAuth2 Proxy enforcing the Keycloak
`/observability-admin` group. Containers run as rootless `praxis-svc` Quadlet
services. This guide installs telemetry transport and the
[operational dashboards and alerts](monitoring.md). Benchmark scenarios are
separate; available panels do not imply that every source is connected.

## Network and trust

| Connection | Boundary |
| --- | --- |
| Browser → OAuth2 Proxy, TCP 8443 | Private-CA HTTPS, verified OIDC login, administrator-group gate, restricted source CIDRs. |
| Gateway → collector, TCP 24443 | Private interface only, one gateway IPv4 `/32`, mutual TLS with a separate client CA. |
| OAuth2 Proxy → Keycloak | Verified HTTPS issuer, optional private DNS mapping preserving the canonical issuer name. |
| Perses → query services | Pod-local HTTP; no query/backend port is published. Pod peers are trusted. |
| Gateway services → local relay | Gateway network namespace/loopback and three bounded, read-only service log files. |

The private CA is not installed into workstation trust automatically. Keep its
signing keys off the VMs. Existing gateway and observation service accounts,
host administrators and pod peers are trusted; this is not tenant isolation.
Do not open the receiver or backend query ports to the Internet.

## Prepare inputs

First prepare the [Keycloak test host](keycloak-rhel.md), using the same gateway
and observability addresses. Include the observability **private IP** as a second
address when generating its certificate:

```console
python3 tests/external-services/certificates.py \
  --keycloak "$KEYCLOAK_HOST_IP" --gateway "$GATEWAY_HOST_IP" \
  --observability "$OBSERVABILITY_HOST_IP" "$OBSERVABILITY_PRIVATE_IP" \
  --output "$TLS_DIR"
```

Use a new output directory. Generate these bundles **before** installing the
issuer; generating a different CA later does not update an existing issuer.

Through verified SSH, transfer only `observability/` to the observability VM,
`gateway/` to the gateway, and the issuer's
`/root/enmaas-keycloak-inputs/observability-client-secret` to the observability
VM. Never print the secret or transfer the issuer's complete credentials file.
Install transferred files under root-owned directories with mode `0700` and
file mode `0600`:

| Host | Prepared file paths |
| --- | --- |
| Observability | `/root/enmaas-observability-tls/{server.pem,server.key,issuer-ca.pem,telemetry-ca.pem}` and `/root/observability-client-secret` |
| Gateway | `/root/enmaas-gateway-tls/{telemetry-server-ca.pem,telemetry-client.pem,telemetry-client.key}` |

Copy the reviewed repository to both hosts using the
[quickstart checkout procedure](../../quickstarts/remote-gateway-pricetag/README.md#2-copy-the-checkout).
Install the gateway separately before attaching telemetry. Set the IP/CIDR
variables below explicitly in each SSH shell; workstation exports do not carry
over automatically.

## Install on the observability VM

```console
cd ~/secure-single-server
KEYCLOAK_HOST_IP=YOUR_ISSUER_IP
OBSERVABILITY_HOST_IP=YOUR_OBSERVABILITY_IP
OBSERVABILITY_PRIVATE_IP=YOUR_OBSERVABILITY_PRIVATE_IP
GATEWAY_PRIVATE_IP=YOUR_GATEWAY_PRIVATE_IP
WORKSTATION_CIDR=YOUR_WORKSTATION_IP/32
sudo dnf install -y python3 python3-pyyaml openssl
sudo python3 tests/external-services/observability-host.py \
  --issuer "$KEYCLOAK_HOST_IP" --observability "$OBSERVABILITY_HOST_IP" \
  --private-ip "$OBSERVABILITY_PRIVATE_IP" \
  --client-secret-file /root/observability-client-secret \
  --output /root/enmaas-observability-inputs
sudo tests/external-services/install-observability \
  /root/enmaas-observability-inputs \
  /root/enmaas-observability-tls/server.pem \
  /root/enmaas-observability-tls/server.key \
  /root/enmaas-observability-tls/issuer-ca.pem \
  /root/enmaas-observability-tls/telemetry-ca.pem \
  "$OBSERVABILITY_HOST_IP" "$GATEWAY_PRIVATE_IP/32" "$WORKSTATION_CIDR"
```

Optional `--issuer-name identity.example.test --issuer-private-ip 10.0.0.10`
must match the issuer's canonical name and certificate. `--issuer` is the
proxy's backchannel to the issuer and must be an RFC1918 address or the host
loopback `127.0.0.1` (which the local lab uses); any issuer address is
accepted together with `--issuer-name`/`--issuer-private-ip`, which pins the
private backchannel instead. `--observability` is the browser-facing endpoint:
use the address the workstation uses to reach this host, public or private;
firewall rules must independently admit only the workstation. Routing/DNS must
already
exist. The installer adjusts firewalld, not cloud security groups. Cloud rules
must independently allow only the intended browser and private telemetry sources.

Open `https://YOUR_OBSERVABILITY_IP:8443` after trusting the supplied CA. The
fixture administrator can sign in; Alice and Bob must be denied. A healthy HTTPS
listener alone does not prove SSO authorization works.

## Attach the gateway relay

Run on the existing gateway VM. Installation restarts Praxis, PriceTag Metering
and MaaS to attach bounded log files and the telemetry relay.

```console
cd ~/secure-single-server
OBSERVABILITY_PRIVATE_IP=YOUR_OBSERVABILITY_PRIVATE_IP
sudo dnf install -y python3 python3-pyyaml openssl
sudo python3 tests/external-services/telemetry-agent.py \
  --observability-private-ip "$OBSERVABILITY_PRIVATE_IP" --host-id gateway-1 \
  --output /root/enmaas-telemetry-inputs
sudo tests/external-services/install-telemetry-agent \
  /root/enmaas-telemetry-inputs \
  /root/enmaas-gateway-tls/telemetry-server-ca.pem \
  /root/enmaas-gateway-tls/telemetry-client.pem \
  /root/enmaas-gateway-tls/telemetry-client.key
```

Use a distinct, nonsecret `--host-id` for each gateway. The relay scrapes Praxis
and PriceTag metrics, accepts emitted OTLP traces and reads three service logs.
A hardened systemd timer samples aggregate Linux host counters every 15 seconds.
The collector's namespace root maps to the unprivileged service account; it is
not a privileged container. Host sampling runs as `praxis-svc`, using systemd
credentials and a single permitted destination IP.

The relay does not enable missing application instrumentation. Trace emission
and application metric availability need an inference acceptance test with the
chosen gateway image. It does not provide database query profiling or per-service
container statistics by itself.

## Retention, privacy and acceptance

- **Privacy:** remove bodies, arbitrary attributes, user identities, URLs and
  trace event text before export. Preserve reviewed label names and correlation
  IDs. Allowed label values and metric names still come from trusted senders;
  this is not an arbitrary-data exfiltration prevention boundary.
- **Capacity:** collector queues/retries and memory are bounded. Drops remain
  possible during overload or prolonged storage failure; no durable spool is
  promised. Each source log rotates at 10 MB; each parsed entry is capped at 64 KiB.
- **Storage:** Prometheus has 48-hour/512-MB retention; Loki and Tempo use
  24-hour retention settings. Their deletion is asynchronous, not a disk quota.
  All stores use local volumes. Backup, rotation and HA are not implemented here.
- **Sessions:** OAuth2 Proxy uses a five-minute cookie lifetime and one-minute
  refresh interval. The tests prove login-time group enforcement, not immediate
  revocation of an already established administrator session.
- **Acceptance:** test administrator allowance and ordinary-user denial, wrong
  CA/client rejection, all three stored signals, private-port reachability,
  retention, reboot and SELinux on the target RHEL hosts. Inspect partial
  installations rather than deleting markers or data to force a retry.

Local unit tests and disposable Podman tests cover generated configuration,
real OIDC/PKCE/group enforcement, telemetry mTLS, privacy, payload bounds,
Perses provisioning and signal storage. They do **not** qualify the RHEL
Quadlet lifecycle, cloud/firewalld packet paths, retention over time or browser
cookie behavior. See the [test commands](../../../tests/external-services/README.md).


## Host samples and diagnostic access

The host timer reads aggregate `/proc/stat`, `/proc/meminfo`, load averages and
`/var` filesystem capacity. It does not read process arguments, environments or
user identities. It sends directly over mTLS using systemd credentials and a
restricted destination IP. Inspect it on the gateway:

```sh
sudo systemctl start enmaas-host-metrics.service
sudo systemctl status enmaas-host-metrics.timer enmaas-host-metrics.service --no-pager
sudo journalctl -u enmaas-host-metrics.service -n 20 --no-pager
```

Memory unavailable is total minus `MemAvailable`; cached memory is reported
separately and must not be added to unavailable. Filesystem sampling does not
measure external RDS storage. Use a stable nonsecret `--host-id` per gateway.

The relay reads only the three configured service log files, with persistent
offsets in a separate volume. It begins at the end of a new file. Rotation,
crashes and receiver outages can lose or duplicate telemetry. Exported log text
is replaced by a constant; inspect original messages over SSH. No journal group
or Podman socket is granted to the relay.

Perses proxies Tempo/Loki queries through the same observability SSO gate. Trace
IDs support detail navigation, while bodies, arbitrary error text, identities and
span-event text are removed. Missing sampled traces or logs do not establish
missing accounting records; reconcile PriceTag's ledger separately.

The optional `host-metrics-runtime.py` probe exercises generated temporary systemd
units in the **local Podman VM only**. Read its prerequisites before running: it
uses the local test service account and port 24443. Do not stop unrelated running
fixtures to make space. It does not qualify a supplied RHEL host.

See [telemetry topology](topology.md) and [certificate scope](certificates.md#telemetry-credential-scope)
for the trust boundaries. The telemetry client CA authenticates a trusted producer;
it does not bind individual metric labels to a certificate subject.
