# Gateway telemetry agent

**Qualification in progress.** The pinned Collector accepts this configuration and its boundary tests pass. Local tests verify real Praxis metrics and fresh traces through the mTLS relay, with authorization/cookie sentinels absent from stored trace details. Four authenticated Perses panels receive finite samples. Mock inference/accounting and telemetry through gateway restarts pass locally; full RHEL installation remains unqualified. A separate restricted systemd sampler collects aggregate host resources; sampling and exact-value transport pass locally, and four host-resource panels passed authenticated browser checks with real Linux samples. The generated timer also passed in the local Podman Linux VM, including systemd credential loading and an enforced negative IP-allowlist test; RHEL execution remains unqualified. Restricted file-log ingestion and the authenticated log table pass locally.

The rootless relay shares the gateway's network namespace. It scrapes the private Praxis admin metrics endpoint and PriceTag metrics, accepts Praxis OTLP on loopback, applies the same attribute restrictions as the receiving collector, and exports over mTLS. Nothing is published from the gateway relay to the host network.

The receiving collector runs on the separate **observability node**. This guide installs the **agent on the gateway node**; both are needed. See [collector placement](topology.md#why-there-are-two-collectors).

## Prepare on the gateway

Use the observability VM's **private IPv4 address**, covered by its server certificate. Apply the [peer network rules](network.md) first. Copy the gateway [certificate bundle](certificates.md) to `/root/enmaas-tls`.

From the installed repository on the gateway:

```sh
sudo python3 tests/external-services/telemetry-agent.py \
  --observability-private-ip "$OBSERVABILITY_PRIVATE_IP" \
  --output /root/enmaas-telemetry-inputs
```

## Install

This restarts the gateway, MaaS and PriceTag once. Use an idle test deployment; existing requests can be interrupted.

```sh
sudo tests/external-services/install-telemetry-agent \
  /root/enmaas-telemetry-inputs \
  /root/enmaas-tls/telemetry-server-ca.pem \
  /root/enmaas-tls/telemetry-client.pem \
  /root/enmaas-tls/telemetry-client.key
```

The installer stores credentials as service-account Podman secrets and creates a gateway Quadlet drop-in for the local OTLP endpoint. That drop-in survives provider-config regeneration. The relay follows gateway restarts; the gateway does not require successful remote telemetry delivery to serve requests.

## Boundaries and failure behavior

- **Local:** OTLP binds `127.0.0.1:4317` inside the gateway network namespace. The relay can access the private admin endpoint and is therefore a trusted deployment component.
- **Remote:** HTTPS 24443 validates the observation server certificate and presents the dedicated telemetry client certificate. The receiver validates its client CA. No TLS verification bypass is configured.
- **Storage:** the relay has no persistent export queue. It mounts the selected log directory read-only and keeps file offsets in a separate volume. Memory is limited, the export queue is bounded, and retries expire after 30 seconds. Extended outages can lose telemetry; accounting remains a separate PriceTag path.
- **Data:** configured transforms remove request bodies, arbitrary headers and unsupported attributes. Retained metric names and allowed label values still need review against actual producers. Do not treat this as a universal secret detector.
- **Partial failure:** the installer leaves a marker and refuses to overwrite existing secrets/units. Inspect state before retrying; it never deletes application databases or credentials.

## Acceptance still required

1. Verify native RHEL Quadlet startup, gateway restart behavior and SELinux permissions.
2. Qualify the local-tested inference and accounting path on RHEL, including PriceTag metrics.
3. Repeat the locally verified traffic/resource/log/trace dashboards on RHEL; benchmark phase markers still need integration. Imported reference dashboards are not automatically qualified.
4. Disconnect the receiver and measure queue/drop behavior while inference and accounting continue.
5. Qualify the host-sampling timer and restricted log collection before the full RHEL benchmark.

## Aggregate host measurements

The testing installer also installs `enmaas-host-metrics.timer`. Every 15 seconds, a short-lived Python service reads aggregate `/proc/stat`, `/proc/meminfo`, `/proc/loadavg` and filesystem capacity for `/var`. It sends OTLP directly over mTLS. It does not inspect process arguments, environment variables, file contents, container sockets or user identities.

The service runs as `praxis-svc` with no capabilities, a read-only filesystem, private temporary/device directories and outbound IP access restricted to the observation host. systemd `LoadCredential` supplies the certificate/key from root-private files; credentials are not command arguments or environment values. The timer does not overlap an already-running sample; a failed delivery is logged and the next sample retries independently. There is no persistent queue.

| Measurement | Interpretation |
| --- | --- |
| CPU seconds by state | Cumulative aggregate counters; compute rates for utilization. Guest time is not counted twice. |
| Memory total, available, unavailable, cached | Unavailable = total − `MemAvailable`; cached is separately reported and must not be added to unavailable. This avoids calling all cache occupied application memory. |
| Load 1/5/15 minutes | Kernel load averages; not CPU percentages. |
| `/var` filesystem total, used and available | Tracks the filesystem holding the default application data. Used and available differ from total because reserved space is separate. External RDS storage is not measured here. |

Inspect the installed sampler on the gateway:

```sh
sudo systemctl start enmaas-host-metrics.service
sudo systemctl status enmaas-host-metrics.timer enmaas-host-metrics.service --no-pager
sudo journalctl -u enmaas-host-metrics.service -n 20 --no-pager
```

Local evidence covers real Linux sampling, exact values/state labels through the pinned collector, and actual generated-timer execution as `praxis-svc`. A separate mTLS receiver observed two samples, and excluding its IP from the allowlist blocked delivery. This validates network-policy enforcement in the local Linux VM, not RHEL SELinux, reboot behavior or capacity. Those remain acceptance checks on the new VM.

## Trace inspection

Perses provisions **enMAAS gateway — traces** using its server-side Tempo datasource. Open a request link in the table to inspect its span timing on the detail dashboard. Stored trace search, pointer navigation and span rendering pass locally. Tempo remains private; browser access goes through the same observability SSO group gate as metrics. No separate Tempo port needs opening.

Traces are sampled diagnostics, not the accounting ledger. The collector replaces span names with `request`, removes request bodies, identity/header attributes, links and event text, and retains timing, IDs and reviewed attributes. Filter names and arbitrary error text are therefore not available in this view. Absence of a trace does not prove that an inference request was rejected or unbilled.

The datasource follows the [Perses Tempo model](https://perses.dev/plugins/docs/tempo/model/) and uses the [Perses datasource proxy](https://perses.dev/docs/perses/latest/concepts/proxy/); no browser-direct backend URL is configured.

## Restricted container logs

The telemetry installer switches the gateway, MaaS and PriceTag to Podman's `k8s-file` log driver and restarts those services. Each has an explicit log file under the service account's private `~/.local/share/enmaas-telemetry-logs` directory and a 10 MB maximum. The relay mounts only that directory read-only, not the system journal, container storage or Podman socket.

The collector's file receiver parses container records and applies the privacy transform before sending them over mTLS. Exported records contain service name, timestamp, severity and a constant body. Original message text, paths, headers and user identities are removed; use SSH for original service messages. The collector necessarily reads original messages locally, so it remains a trusted component.

The collector runs as UID 0 **inside its rootless user namespace**, mapped to `praxis-svc` on the host, with no capabilities and a read-only root filesystem. This permits reading service-owned files without granting host-root or system-journal access. Its only writable application mount stores file offsets. The receiver starts at the end of new files; persisted offsets support restarts. Rotation, crashes and receiver outages may lose or duplicate events; logs are not the accounting ledger.

The pinned image accepts the file receiver and persistent-offset configuration. Tests check explicit file selection, record bounds, log-size settings and restricted mounts. The local runtime test delivered real Podman stdout through mTLS to Loki, removed the original sentinel text, resumed after a receiver restart and an in-place truncation, and observed exactly three events. The authenticated Perses log table and anonymous/manager/member denials also pass. RHEL SELinux and native service startup remain unqualified.

The earlier journal-reader prototype was removed after the local service account could not access its journal without broader privileges. No journal-group membership is required.


Open **enMAAS gateway — service events** in Perses to inspect sanitized records. The Loki datasource is server-proxied through the same SSO gate as metrics and traces; no Loki query port is public. Expand an event to inspect its retained service metadata. Raw message text is deliberately unavailable remotely.

## Local systemd timer qualification

On a workstation using `podman machine`, stop the local lab pod first: its collector already occupies port 24443. The Linux VM must have the non-root `praxis-svc` test account prepared by the gateway setup. This test targets only that local VM, never a supplied RHEL host:

```sh
python3 tests/external-services/host-metrics-runtime.py
```

It uses disposable certificates and temporary units under `/run`, starts the generated 15-second timer, requires two mTLS deliveries, and verifies that excluding the receiver IP prevents another delivery. It then removes its units, certificate files and receiver. It does not stop unrelated services automatically. Restart the local lab pod afterward if needed.
