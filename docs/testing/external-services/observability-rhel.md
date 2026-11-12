# Dedicated RHEL observability test host

**Qualification in progress.** The generated units pass the real Quadlet generator; the pinned collector accepts the configuration. The complete RHEL installation and telemetry redaction under load remain unqualified.

## Host prerequisites

Use the reviewed repository checkout on this host. Install the tools needed to generate inputs before running the installer:

```sh
sudo dnf install -y python3 python3-pyyaml openssl
```

## Prepare protected inputs

Use the observation VM from the AWS lab. Obtain the **observability client secret only** from the issuer setup and save it in a root-owned mode-`0600` file on this host. Do not copy issuer administrator credentials or its entire credentials file.

From the repository on the observation host:

```sh
sudo python3 tests/external-services/observability-host.py \
  --issuer "$KEYCLOAK_HOST_IP" \
  --observability "$OBSERVABILITY_HOST_IP" \
  --private-ip "$OBSERVABILITY_PRIVATE_IP" \
  --client-secret-file /root/enmaas-inputs/observability-client-secret \
  --output /root/enmaas-observability-inputs
```

Supply a server certificate covering both the public observation IP and its private IP, its private key, the public issuer CA, and a dedicated public telemetry client CA. The telemetry CA should authorize only gateway collectors. Keep CA signing keys on the workstation; do not deploy them to either service host.

For a DNS issuer with private peer connections, follow the optional
[private issuer resolution](oidc.md#private-issuer-resolution)
configuration before generating inputs. Keep both clients on the same issuer name.

## Install

```sh
sudo tests/external-services/install-observability \
  /root/enmaas-observability-inputs \
  /root/enmaas-tls/server.pem \
  /root/enmaas-tls/server.key \
  /root/enmaas-tls/issuer-ca.pem \
  /root/enmaas-tls/telemetry-ca.pem \
  "$OBSERVABILITY_HOST_IP" "$GATEWAY_SOURCE_CIDR" "$WORKSTATION_CIDR"
```

The final argument accepts additional browser source CIDRs. TCP 8443 is permitted from those sources; TCP 24443 binds only the configured private interface and is permitted only from the gateway private source address. Set `GATEWAY_SOURCE_CIDR` to the gateway private IP followed by `/32`. Matching AWS security-group rules are still required. The script does not change AWS resources. On EC2, public-IP traffic is translated to the private interface, so the private bind alone does not block public traffic: the source-restricted security group and host firewall are both required.

## Services and access

| Service | Responsibility and boundary |
| --- | --- |
| OAuth2 Proxy | Public HTTPS 8443, verified Keycloak issuer, PKCE, secure cookies, explicit `/observability-admin` group. |
| Perses | Dashboards behind OAuth2 Proxy; its direct port is not published. |
| Prometheus | Metrics, 48-hour/512 MB retention configuration; no public query endpoint. |
| Loki | Logs, 24-hour retention configuration; no public endpoint. |
| Tempo | Traces, 24-hour retention configuration; no public endpoint. |
| OpenTelemetry Collector | HTTPS 24443 requires a certificate signed by the telemetry CA; forwards signals to private backends. |

All containers run in the service account's rootless pod with dropped capabilities. They share a trusted internal network namespace; these are not mutually isolated tenants. PostgreSQL is not needed on this host. Configurations and private keys are mounted only into consuming containers as Podman secrets.

The Keycloak group is explicitly administered. It is **not yet synchronized with PriceTag administrator roles**. The test administrator is assigned to it; Alice and Bob are excluded. Do not assume that changing a PriceTag role immediately changes observability access.

## Telemetry scope and remaining checks

The collector keeps reviewed attribute names and removes raw log bodies, span/event names, status messages and scope attributes. It is configured to reject a batch when transformation fails. A black-box test against the pinned collector removes sentinels from log bodies, resource/scope/data attributes, span events/links/trace state, metric descriptions/units and exemplars. It covers gauges, sums, histograms, exponential histograms and summaries while checking that signal records and trace IDs survive. Span links and exemplars are deliberately removed; the lab does not offer navigation based on those fields. Metric names and values of retained labels still require an audit against the actual producers before accepting real traffic. Do not send prompts, completions or credentials to the collector. This attribute policy is not a general secret detector for arbitrary values in approved fields.

Run the privacy regression from the repository with a running local Podman engine:

```sh
python3 tests/external-services/redaction.py
```

It starts a temporary mTLS collector, exports only synthetic test data to private temporary files, asserts redaction and removes its container/secrets afterward. No cloud resources or real provider credentials are used.

New receiver configurations set a **4 MiB HTTP request-body limit** and a maximum
export batch of **512 records**. The regression verifies explicit rejection of
oversized plain and gzip-compressed requests, then successful export of normal
logs, traces and metrics. The request limit applies before privacy transforms;
it is separate from record-count batching and container memory limits.

These bounds do not limit the number of distinct metric names or label values a
trusted producer can send over time. Per-producer identity enforcement,
cardinality controls, store disk limits and long-outage behavior remain separate
qualification gaps. Existing running collectors need regenerated configuration
and a controlled restart to receive these settings.

The initial dashboard verifies transport with a synthetic metric. Dedicated gateway traffic, host-resource, trace-detail and sanitized-log dashboards pass local browser tests with actual emitted signals. Imported reference dashboards are not automatically qualified, and benchmark phase markers are not yet integrated into Perses. These local results do not qualify the separate RHEL host installation.

Before RHEL acceptance, repeat the locally tested persona logins, existing-session group revocation, client-certificate rejection and backend access denials. Also qualify collector restart/drop behavior, retention deletion and disk growth. Loki and Tempo currently have time retention but no tested disk quota.

The configuration uses OpenTelemetry's [transform processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.165.0/processor/transformprocessor/README.md). Test the pinned version rather than assuming newer documentation describes the deployed behavior.

## Existing firewall configuration

Before creating service secrets, the installer audits runtime and permanent
firewalld rules. It repeats the audit before starting services. Extra ports,
services, unreviewed policies, direct rules and broader rich rules stop the
installation. Existing rules are never removed automatically. The standard
IPv6 host policy is accepted only with its reviewed ICMPv6 behavior; SSH and
DHCPv6 service definitions are checked rather than trusted by name.

If the audit stops, review the named configuration on the dedicated VM before
retrying. Do not disable firewalld or remove the IPv6 policy to bypass the check.
This audit does not cover independently installed nftables rules or replace AWS
security-group checks and external packet-level deny probes. Its live RHEL
qualification is still pending.
