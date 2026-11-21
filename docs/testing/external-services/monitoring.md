# Operational monitoring

The [observability installer](observability-rhel.md) provisions the main overview
and eight detail dashboards. These describe service operation; benchmark phases,
offered load and estimated people/bots belong in separate benchmark tooling.

## Read the overview

1. **Check telemetry age and alerts.** Missing measurements mean unavailable,
   not zero utilization. An empty alert table alone does not prove health.
2. **Choose hosts.** CPU and memory are normalized separately for each host.
   All vCPUs together equal 100%; individual container CPU is measured in cores.
3. **Compare traffic and errors.** Public throughput counts responses once.
   Internal provider/authentication calls are additional hops, not extra users.
   3xx, 4xx and 5xx have separate panels and links to filtered service events.
4. **Find the constrained service.** Compare gateway, MaaS and PriceTag memory
   with each container's configured limit. Percentages are never added together.
5. **Check storage and recording.** Database lock waits, metering pool waits and
   failed usage inserts are different signals. Successful inference does not
   prove that every usage event was stored.

Host selectors affect resource panels. Gateway traffic and metering application
metrics are currently **fleet-wide**; their exporter labels are not a verified
host mapping. The database selector is independent of the host selector.

| Dashboard | Question answered |
| --- | --- |
| Main monitoring | Which resource, policy or telemetry condition needs attention? |
| Gateway traffic | Which public/provider routes return errors or take longer? |
| Host resources | How much CPU, available memory and filesystem capacity does each host have? |
| Component resources | Which service instance consumes CPU, memory or process/thread capacity? |
| Database & usage metering | Is PostgreSQL or the usage-recording path under pressure? |
| Alerts | Which conditions are pending/firing, and where should I investigate? |
| Inference traces | Which sampled inference spans failed or took longer? |
| Trace detail | What spans belong to the selected trace? |
| Service events | Which sanitized events match the selected service/status? |

Inference duration includes provider and streaming time. It is **not** proxy
overhead or time to first token. Logs exclude original messages, prompts and
credentials. Missing filtered logs/traces do not establish that no failure
occurred: compare request counters, instrumentation coverage and sampling.

## Collection coverage

| Source | Available collection | Boundary / remaining integration |
| --- | --- | --- |
| Linux host | Hardened RHEL timer samples aggregate CPU, RAM, filesystem and load values. | One nonsecret host alias per host. Does not measure another VM or RDS host. |
| Praxis and PriceTag | Gateway relay scrapes their private metrics listeners. | The selected images must expose the expected metric names. Missing source counters stay unavailable. |
| Service containers | `container-metrics.py` converts bounded Podman CPU/memory/PID observations, retaining host/service/replica. | Scheduling and owned-container inventory belong to the local deployment integration; this slice does not add a RHEL container-sampling timer. |
| PostgreSQL | `database-metrics.py` collects nine aggregates with a read-only transaction and three-second statement timeout. | Its collection command targets the local fixture database. Remote PostgreSQL needs its own verified connection/least-privilege role adapter. No CloudWatch/RDS host metrics are collected. |
| Traces and logs | Private relay → mutually authenticated collector → Tempo/Loki. | Gateway instrumentation must be enabled. The event path preserves reviewed structured fields, not raw diagnostic messages. |

Dashboard definitions are not evidence that a source is connected. The isolated
tests feed clearly synthetic data to check queries, labels and rendering. The
full deployment acceptance test must separately demonstrate real source
collection during inference, and reconcile stored usage.

## Alerts and limits

`tests/external-services/monitoring.py` owns the operational rules and thresholds.
They are starting points to review for the deployment, **not service SLOs**:

- Host CPU above 85% for two minutes; low available memory/filesystem space.
- Container memory above 80% of its own limit; gateway file descriptors above 80%.
- Database pool waiting, usage-insert errors, high 5xx ratio and inference delay.
- Policy denials, failed scrapes, and absent or stale telemetry.

Policy warnings can mean enforcement is working. Slow models can legitimately
trigger the inference-duration warning. Trace sampling and telemetry queues can
drop observations during overload. There is no notification delivery or
Alertmanager integration in this slice; firing rules appear in Prometheus/Perses.

## Inspect and test

Export definitions without modifying a running deployment:

```console
python3 tests/external-services/monitoring.py --output .state/monitoring-preview
python3 -m unittest discover -s tests/external-services/unit -v
```

With the pinned service images available in local Podman:

```console
python3 tests/external-services/test-alert-rules.py
python3 tests/external-services/telemetry-runtime.py
```

The first checks alert timing/recovery, host normalization, status classes and
missing-data behavior. The second provisions every dashboard and checks all
panel queries against known signal values in Prometheus, Loki and Tempo.

Optional browser rendering and selector checks use the existing locked browser
dependencies. Only this variant publishes a temporary **loopback-only** Perses
HTTP port containing synthetic data; it is not an authentication test:

```console
npm ci --prefix tests/pricetag/browser --ignore-scripts
tests/pricetag/browser/node_modules/.bin/playwright install chromium
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  python3 tests/external-services/telemetry-runtime.py --browser
```

The fixtures remove their temporary pods, networks and secrets, without touching
the running gateway. Browser screenshots are local ignored files under
`.state/monitoring-browser/`. SSO/TLS authorization is tested separately by the
[external-service security probes](../../../tests/external-services/README.md).
