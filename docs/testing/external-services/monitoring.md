# Monitor a mock benchmark

Use the **Main monitoring** dashboard to follow traffic, policy decisions and resource pressure together. Open its detail links for gateway traffic, component resources, host resources, usage metering, traces and service events.

This is an administrator-only observability surface. The local fixture uses verified private-CA TLS and an SSO group check; telemetry ingestion requires a client certificate. Store ports are private. Its shared Podman namespace does **not** qualify isolation between separate RHEL hosts.

## Run the workload

Complete the [local gateway and telemetry setup](local-podman.md) first. From that same checkout and terminal:

```sh
export ENMAAS_LAB_INSTANCE=external-demo
export LAB_STATE="$PWD/.state/$ENMAAS_LAB_INSTANCE"
export PRAXIS_IMAGE=quay.io/opendatahub/praxis-experimental:sha-5667c4acecd0
python3 tests/external-services/local.py check
python3 tests/external-services/local-benchmark.py \
  --praxis-image "$PRAXIS_IMAGE" --scenario sustained --duration 600 --cycles 1
```

The runner accepts **mock backends only**, checks local ownership and available resources (by default at least 30% and 8 GiB free disk), records phase boundaries, verifies exact per-user tokens/USD, and restores policy while retiring its test keys. `Ctrl-C` requests bounded cleanup. Check the printed report directory's `cleanup.json` before repeating an interrupted run. Read-only host/container probes have three bounded attempts; repeated failures stop the run. Successful safety checks are spaced five seconds apart.

| Phase | Users × parallel requests | Offered requests/second | Gateway rate limit | Purpose |
| --- | --- | --- | --- | --- |
| Baseline | 1 × 1 | 2 | 1,000/s | Establish low-load behavior. |
| Team | 25 × 1 | 15 | 1,000/s | Mix people, bots, clients and model protocols. |
| Parallel | 25 × 4 | 30 | 1,000/s | Increase simultaneous work per identity. |
| Sustained high | 25 × 4 | 60 | 1,000/s | Hold higher offered load; observe actual throughput and headroom. |
| Rate limited | 25 × 4 | 30 | 5/s | Show deliberate admission rejection and its warning. |
| Recovery | 1 × 1 | 2 | 1,000/s | Verify successful requests after removing the restriction. |

Each phase lasts **600 seconds by default**. `--duration 65` is a smoke run; `--cycles 2` repeats the sequence twice. Cycles are bounded to 1–12, with at most 20 hours of scheduled load. Test keys cover the planned duration plus one hour and are revoked during cleanup. Offered rate is a target, not a promised completed rate: per-user concurrency and response duration can limit admission. The older burst sequence remains available with `--scenario traffic --offered-rps 30`.

Mock profiles use configured OpenAI-compatible and Anthropic Messages routes, varied delays/token counts, familiar client user agents, and fictional people/bots. They are not real inference or realistic GPU capacity measurements. People have $300 monthly budgets; bots have explicit $1,000 overrides. A separate [Alice team setup](local-dashboards.md) exposes 15 people and 10 bots for manager-view inspection after a successful run.

## Read the dashboards

Open [Main monitoring](https://localhost:19443/projects/praxis/dashboards/enmaas-monitoring?start=1h&refresh=15s). Use the [local login instructions](local-dashboards.md#2-trust-the-lab-certificate-and-obtain-the-password).

The **Main monitoring** dashboard contains operational measurements only:

- **Gateway:** public response rate by route/status, concurrent requests, mean stream duration, internal upstream outcomes and file-descriptor utilization.
- **Hosts:** select one or more hosts. CPU, memory pressure and disk capacity remain separate series per host; there is no fleet-wide host percentage.
- **Gateway resources:** CPU cores, memory and configured memory limits per selected host and instance.
- **MaaS Standalone and PriceTag Metering:** separate CPU-core and memory timelines for each service, host and instance. Each memory chart includes its configured ceiling; compare both services with traffic and database activity.
- **Database:** select a PostgreSQL source independently from Host. View connections, size, committed transactions, lock waiters, deadlocks and temporary writes.
- **Usage recording and alerts:** fleet-wide insert failures, pool waiting and operational alerts. Generator alerts are excluded.

The **Host** selector applies to resource charts only. Gateway HTTP and accounting exports currently lack a verified host mapping, so those sections explicitly remain fleet-wide. The collector’s `instance` label is not a gateway-host identity. Per-host HTTP attribution requires adding a bounded host/gateway identity at collection; do not infer it from a scrape endpoint.

[Benchmark](https://localhost:19443/projects/praxis/dashboards/enmaas-benchmark?start=2h&refresh=15s) is a concise results dashboard: dispatch/success rates, offered versus completed traffic, concurrency and its ceiling, skipped admissions, failures, load stages and generator alerts. Cards describe the phase at the selected time, not whole-run totals. Exact accounting, resource peaks and cleanup results are in the [offline report](report.md).

**Database collection:** the current local fixture collects PostgreSQL engine metrics from its local database. The panels discover sources carrying those engine metrics; they can display RDS PostgreSQL through the same metric contract once a remote collector is configured. Switching the application to an external database does **not** automatically install monitoring. Remote database collection and RDS CloudWatch ingestion are not implemented by these dashboard changes. RDS CPU, IOPS, storage latency, free storage and credits need a CloudWatch adapter with read-only AWS permissions; do not substitute the gateway VM’s host metrics.

Each chart explains its units and scope. Service CPU uses cores, memory uses MiB/GiB, and only actual limits have utilization percentages. Mean stream duration includes provider response time; it is not proxy overhead or first-token latency. The currently exported telemetry does not provide a qualified latency histogram or first-token distribution. Empty idle latency is undefined, not zero.

| Detail | What it establishes | Important limit |
| --- | --- | --- |
| Gateway traffic | Public routing channels, response classes, concurrent requests and upstream activity | Internal hops must not be added to public counts as unique requests. |
| Components | Per-container CPU, memory against its limit, and process/thread count | Local Podman sampling; remote component sampling is not yet installed. CPU is shown in cores; memory is shown in bytes and against a configured limit only when one exists. Host percentages remain separate. |
| Host resources | CPU, available memory, filesystem capacity and load | On macOS this is the Linux Podman VM, including observability. On macOS the generator/browser run outside that VM; on native Linux they share the host. Cache is not treated as exhausted memory. |
| Database & usage metering | PriceTag connection-pool waits, event ingestion, insert latency/errors and quota decisions | These are PriceTag's pool metrics, not a complete PostgreSQL diagnostic suite. |
| Inference traces | Real sampled inference spans, duration and trace navigation | Sampling can omit individual requests. Raw paths, headers, bodies and identity values are removed. |
| Service events | Sanitized Loki events | Local workload events are synthetic operational markers, not captured application errors. |
| Alert detail | Current firing/pending rules, history and response guidance | Prometheus evaluates rules; there is no Alertmanager or notification delivery in this lab. |

Charts use the selected historical window; **alert tables use instant queries at the selected end time**; keep the range ending at now for live firing state. A 15-minute view becomes empty after an old run. Select the run's time window or start new traffic. Zero is a valid value for pool waits and error counters; undefined idle latency is not zero latency.

The runner samples host and component resources during load. For a bounded period of interactive viewing afterward:

```sh
python3 tests/external-services/local-host-metrics.py --samples 240 --interval 30
```

That lasts up to two hours. After sampling stops, the host-telemetry rule deliberately warns about stale data; cached exporter values must not imply current health.

## Save the completed run's evidence

Set `RUN_DIR` to the private report directory printed by the runner, then verify that run:

```sh
export RUN_DIR=/absolute/path/to/the/printed/lab-run-directory
python3 tests/external-services/monitoring-result.py "$RUN_DIR"
```

This requires the complete sustained sequence, exact ledger reconciliation, cleanup and fresh phase telemetry. It checks that the policy alert fired during the rate-limited phase and recovered afterward. It writes a compact result with a fixed-time dashboard link, plus `capacity-summary.json` with measured per-phase headroom. Use that link to inspect the completed run after live traffic stops; Prometheus retention still limits how long the history is available. Follow the [offline report instructions](report.md) to render resource graphs and that summary as HTML. Rate summaries use two-minute windows, which overlap phase boundaries; maximum mean latency is not a percentile.

## Alerts and interpretation

| Category | Conditions | Response |
| --- | --- | --- |
| Capacity | CPU >85% for 2m; available host memory <15% for 2m or <8% for 30s; component memory >80% of its limit for 1m; gateway FDs >80% for 2m | Reduce load and inspect the affected resource before increasing concurrency. |
| Storage | Available data filesystem <15% for 2m or <8% for 30s | Inspect retained data. This is capacity, not disk latency or retention correctness. |
| Traffic | Mean inference duration >5s for 2m; public 5xx ratio >5% for 1m | Compare upstream delay with local resource pressure. Mean duration includes streaming time. |
| Usage recording | Insert errors within 2m, sustained 15s; aggregate DB pool wait >0.1 seconds/second for 2m | Investigate delivery and pool pressure; successful inference alone does not prove usage-record completeness. |
| Policy | Rate-limit rejection or quota denial, sustained 15s | Expected during intentional policy tests; distinguish it from resource exhaustion. |
| Telemetry | Host sample age >90s, or absent host/gateway/metering metrics, sustained 1m | Restore collection before drawing capacity conclusions. Source timestamps survive the collector, so repeated scrapes cannot make cached gateway metrics appear fresh. Stale-source rules also cover absence, keeping the alert active when old samples age out. |

Thresholds are **test starting points**, not production SLOs. The dashboard links to detail views; alerts do not automatically change policies, scale services, or delete data. Critical capacity thresholds are evaluated with isolated synthetic rule tests rather than deliberately exhausting the workstation.

## Verify data, rendering and navigation

Install the [browser test dependencies](local-podman.md) first. During or shortly after a run:

```sh
python3 tests/external-services/test-alert-rules.py &&
NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/monitoring-dashboard.cjs "$LAB_STATE" --record &&
NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/trace-dashboard.cjs "$LAB_STATE"
```

The monitoring browser test checks provisioned dashboard APIs, finite samples for every non-alert query and all 13 components, rendered information descriptions, detail links, actual inference trace navigation and sanitized log rows. Empty alert lists are allowed when no alert is firing. The trace test requires **inference** traces and verifies that unauthenticated users and ordinary users cannot access the telemetry proxy. Video starts **after authentication**; passwords and login forms are excluded. Evidence remains in the protected fixture directory. Never upload that whole directory: it also contains private keys and sessions.

## CI qualification

The `Local observability qualification` workflow runs contract tests and isolated alert rules on relevant pull requests. Its manual runtime job builds pinned application images and runs the complete local stack with either 65-second or 600-second phases. It also builds the bounded async mock provider and runs two 65-second slow-response phases. It checks exact accounting, alert firing/recovery, generator signals, dashboard queries and browser authorization. No AWS or provider credentials are required, and private fixture state is never uploaded.

The runtime job is prepared but has not yet passed on a GitHub runner. Measure build time, free disk and peak memory before making it a required PR check. [Standard public Linux runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) provide 4 CPUs, 16 GB RAM and 14 GB SSD; image builds and retained telemetry can exhaust disk even when RAM is sufficient. The runtime resource guards remain authoritative. The larger local capacity experiment uses 16 vCPUs and 64 GiB RAM, with browser/generator processes outside the VM; a native Linux CI runner must accommodate those too. CI smoke results are not a substitute for the larger capacity experiment.

## Coverage boundaries

Local testing can qualify query syntax, data delivery, SSO access decisions, trace redaction, rule firing/recovery, mock accounting and browser behavior. It cannot establish RHEL SELinux/firewall behavior, cross-host mTLS isolation, reboot recovery, AWS routing, actual retention deletion, real-provider capacity or GPU saturation.

Network saturation, disk I/O latency, PostgreSQL replication, telemetry queue/drop measurements and notification delivery are not yet covered by these dashboards. No capacity claim should be made for a resource that is not measured. RHEL host sampling exists separately; per-container panels still require a remote sampling implementation and qualification.

## Async capacity staircase

The asynchronous runner uses slow mock responses of **10–120 seconds**. It plans
10 → 100 → 1,000 → 10,000 offered req/s, then recovery, with ten-minute stages.
It stops escalation on request/service errors, unexpected policy denials or more
than 1% skipped admissions. A generator cap is **not** a measured gateway limit.

From the configured local fixture checkout:

```sh
python3 -m venv .state/stress-venv
.state/stress-venv/bin/pip install -r tests/external-services/stress-requirements.txt PyYAML==6.0.3
.state/stress-venv/bin/python tests/external-services/build-stress-provider.py
```

The build prints a content-derived `localhost/enmaas-stress-provider:…` image
name. Pass that exact value as `STRESS_IMAGE`; the runner rejects an image tag
that does not match its current source inputs.

```sh
export STRESS_IMAGE=localhost/enmaas-stress-provider:REPLACE_WITH_PRINTED_TAG
.state/stress-venv/bin/python tests/external-services/local-stress.py \
  --praxis-image "$PRAXIS_IMAGE" --provider-image "$STRESS_IMAGE" \
  --duration 600 --concurrency 5000
```

For integration testing, use `--duration 65 --max-rate 10 --concurrency 512`.
The generator requires at least `concurrency + 256` file descriptors. Check `ulimit -Sn` and `ulimit -Hn`; where the hard limit permits, `ulimit -n 8192` in the benchmark shell accommodates the default 5,000-request cap without changing host-wide settings. Resource
guards remain enabled. Never reduce them to claim a higher throughput.

The runner verifies the loopback-only fixture, creates isolated test identities,
raises the gateway admission limit above each offered rate, replaces only the
owned mock-provider container and restores its original launch command afterward.
It retires the test keys and restores gateway policy. Confirm `cleanup.json`
says `complete: true` before another run. Private recovery metadata and keys must
not be published.

The **Benchmark** dashboard shows generator concurrency, skipped admissions, dispatch/completion rates and load/resource comparisons. These client measurements are distinct from gateway throughput and latency. Its generator cards are unavailable outside a recent async run. Use `--require-stress` with the browser test to require generator data.


After a completed async run, verify its evidence separately from the original six-phase scenario:

```sh
python3 tests/external-services/stress-result.py "$RUN_DIR"
```

Use the private run directory printed by `local-stress.py` as `RUN_DIR`. A
`bounded_limit_observed` result means the test established a limit and recovered;
it does not mean every requested rate was achieved. The report distinguishes
admission-time dispatch rates from completion rates including request draining.


For an explicit memory-sizing experiment, `--gateway-memory-mib` accepts
`512`, `1024`, `2048`, `4096` or `8192` (MiB). This option
changes only the benchmark gateway container limit. The previous limit is restored
afterward. The run summary records the selected limit; do not compare these results
as if they used the default 512 MiB fixture. Host memory and disk checks remain active.

## Capacity and realistic streaming

The Main monitoring dashboard separates per-host pressure from gateway and accounting health. The Benchmark dashboard contains load-client measurements. Request/response bytes and SSE-frame counts are retained in the private run summary; partial transport-failed replies are excluded. These are application frames, not TCP packets.

People/agent equivalents are explicitly assumed workloads: one model request per
minute per person, or ten per minute per busy agent. They are alternative estimates
based on dispatched traffic, not measured seats. See
[traffic and RHEL sizing](../../quickstarts/remote-gateway-pricetag/sizing.md).

The asynchronous mock transmits four bytes of synthetic input/output text per
reported token and four output tokens per SSE delta. This approximates payload and
chunk load without claiming tokenizer accuracy. Duration remains a bounded
10–120 seconds. It does not simulate real GPU computation or every tool/multimodal
payload. Keep a separate accounting reconciliation after each run.

For the richer streaming fixture, increase only the mock backend's ceiling with
`--provider-memory-mib 2048` when the default 512 MiB fixture is the measured
bottleneck. The runner records this setting and restores the original mock launch
on exit. This is test infrastructure, not an increase to the real gateway's
upstream provider capacity. Keep gateway and provider limits separate in reports.

### Verify PriceTag organization views under concurrent queries

The local PostgreSQL fixture has a 2 GiB memory ceiling and a private 512 MiB `/dev/shm` mount. Startup verifies the effective mount size. A small shared pod mount can cause organization aggregation queries to return HTTP 500 even when disk capacity remains available.

With populated mock usage and the local administrator configured:

```bash
NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
node tests/external-services/organization-dashboard.cjs "$LAB_STATE"
```

This read-only check validates fresh SSO, three concurrent API rounds, and populated charts in both Org usage and the organization/team view. It does not replace testing those queries during sustained inference load.

For continuous interactive monitoring, use `local-host-metrics.py --continuous --interval 15` and stop it with Ctrl-C. This collects metrics without generating inference traffic. Host aliases are bounded deployment labels, not addresses. Host histories and summary cards retain one independently normalized value per selected host; stale hosts are excluded. Service CPU and memory must not be summed into a percentage across unrelated instance limits.

### PostgreSQL source

The local sampler reads aggregate statistics for the metering database: connections, active sessions, lock waits, committed/rolled-back transactions, deadlocks, temporary-file writes and database size. The connection limit is server-wide; metering connections are database-specific and include the short-lived sampler connection. Sampling uses a read-only transaction with a three-second statement timeout. No application rows, query text, client addresses or keys are exported.

These are PostgreSQL engine statistics, not RDS CloudWatch measurements. The local fixture uses its existing database role over `podman exec`, without opening a database port. A remote database requires a separately configured least-privilege monitoring identity and transport before these panels can be qualified there.

### Resource selectors and instances

Host detail offers a **Host** selector. Service detail adds **Service** and **Instance** selectors. Keep host aliases stable and unique. Service telemetry uses `(host, component, replica)` rather than combining all copies of a service into one line. The local inventory accepts the existing primary names and bounded replica names such as `enmaas-example-provider--two`; unrelated container names are rejected.

An instance without a memory limit has memory usage but no percent-of-limit value. Service CPU is always CPU seconds per second (cores). Host capacity remains normalized independently for each host. Query tests exercise both an overloaded/idle host pair and identical services on separate hosts.

Offline container reports also separate named replicas and host-tagged samples. When collecting from multiple hosts, retain a stable `host` alias on each resource record; container IDs alone are not globally unique.

### Anonymized database profiling

The optional `local-stress.py --database-profile` flag records per-stage statement costs in the private run directory. PostgreSQL must preload `pg_stat_statements`, and the metering database must have that extension installed. Preloading requires a controlled database restart; this option does not silently change the database or restart it during load.

After a profiled run, render its report:

```bash
python3 tests/external-services/database-report.py "$RUN_DIR"
```

The report lists numeric fingerprints, fixed operation categories, calls, total/mean execution time, buffer hits/reads and temporary writes. It exports no SQL text, bind parameters or identities. Categories are heuristic. The maximum is **since statistics reset**, not a stage maximum; no percentile is inferred. Resets and entry eviction invalidate the interval comparison. Buffer reads are not necessarily physical disk reads, and database execution time does not include waiting for a PriceTag connection-pool slot.

Use the Benchmark dashboard's aligned load/resource charts to compare achieved requests/s with CPU cores, memory, pool waiting and inference duration. Mixed-unit charts use separate axes and formatted hover values; their legend aggregate values are intentionally hidden. Choose the run's fixed start/end window to keep recovery and unrelated runs distinct.

## Human and bot traffic estimates

Benchmark includes separate **Expected active people** and **Expected active bots**
charts. Each shows offered and dispatched workload equivalents. The assumptions
are one request per minute per active person and ten per minute per active bot.
These are alternative workload estimates, not observed users or qualified seat
counts; do not add them. A mixed population needs a measured human/bot traffic
split and request frequencies.

## Record populated dashboards

Run from the local fixture checkout with its private CA trusted by Node:

```sh
export NODE_PATH="$PWD/tests/pricetag/browser/node_modules"
export NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem"
node tests/external-services/monitoring-dashboard.cjs "$LAB_STATE" --record --require-stress
node tests/external-services/metering-walkthrough.cjs "$LAB_STATE"
```

For a completed benchmark outside the last two hours, append `--at` with its
end time in Unix seconds and `--window-minutes` with the measured duration to the
monitoring command. Add `--require-continuous-data` to reject gaps beyond the
initial rate warm-up. Historical checks require retained samples.

Videos are saved under `monitoring-video/` and `metering-video/` inside the private
fixture directory. Authentication occurs before recording; no keys or passwords
are displayed. The PriceTag walkthrough visits administrator usage, organization
usage and Alice's team. It requires existing populated views and changes no
identities. Both recordings scroll forward through each page. Review recordings
before sharing: they contain dashboard usage and account display names.

## Inspect HTTP outcomes

Main monitoring provides a **Response classes** multi-select for the main public
throughput chart. Defaults are **2xx successes**, **1xx informational** and
**unclassified** responses. Add 3xx, 4xx or 5xx individually, or select All.
Praxis exports status classes: 2xx includes HTTP 200, 201 and 204; it is not an
exact-code 200 counter.

Separate, adjacent **3xx**, **4xx** and **5xx** panels remain independent of that
filter. Each shows requests/second over two minutes. A zero needs observed public
request counters and fresh gateway telemetry; missing telemetry stays unavailable.
4xx includes expected authentication, quota and rate-limit enforcement.
Fast failures can lower aggregate mean latency: compare successful throughput
and failure counts before treating a shorter duration as an improvement.

The throughput and status panels include a **Logs** link to the Loki-backed
service-events dashboard. The status panels also provide **View 3xx/4xx/5xx logs**:
these select the gateway service, matching structured HTTP status codes and the
current time window. The routed-throughput panel in Gateway detail also includes **View 5xx logs**.

On the log page, use **Service** and **HTTP status** to broaden the search. A
filtered empty result means no matching collected events, not zero gateway errors.
HTTP filtering requires retained `http.response.status_code` metadata; the current
local relay does not mount gateway application logs. Metrics and sampled traces
remain available when those logs were not collected. Sanitized diagnostic events
are not exhaustive access logs, and a response need not have a corresponding event.
A time-window link does not establish exact trace correlation without matching
trace IDs. Original messages, request bodies and authorization headers remain
excluded.

### Host, service and instance labels

Resource legends identify **Host**, **Service** and **Instance** separately. In the
local fixture, `gateway` identifies the Podman VM, `gateway-sso` identifies its
Praxis gateway container, and `primary` is the default instance alias. It does not
indicate leader election or a failover relationship. The **Database & usage metering**
dashboard covers PostgreSQL and usage-event processing; spending views remain in
PriceTag. Its existing `enmaas-accounting` URL is retained for link compatibility.

### Verify populated persona views

After provisioning the local Super admin, Alice manager and Bob individual with
mock usage, run this read-only check from the checkout (set `LAB_DIR` to the
private fixture directory):

```sh
NODE_EXTRA_CA_CERTS="$LAB_DIR/ca.pem" node tests/external-services/persona-dashboard.cjs "$LAB_DIR"
```

It verifies organization/team/personal data, rejects out-of-team API requests and
checks that a personal usage query cannot select another user. It does not create
missing identities or generate usage. An unprovisioned Bob correctly fails SSO;
configure the persona before recording. Screenshots and a nonsecret result summary
are written inside the private fixture directory. Playwright must be installed as
for the other dashboard checks above.

### Benchmark client failures

The Benchmark dashboard also alerts when more than **5% of terminal outcomes**
are unsuccessful for one minute, after at least 100 outcomes. In-flight requests
are excluded. HTTP rejections, transport failures and response-validation failures
all count; inspect the report to distinguish them. This does not assign fault to
the gateway. The alert clears on fresh, healthy recovery data and stops using
samples older than 30 seconds.

### MaaS Standalone and PriceTag Metering

Main monitoring includes named CPU and memory timelines for both services.
Memory panels also show each container’s configured limit; separate lines retain
host and instance identity. The Benchmark dashboard includes both services in
its load-versus-CPU and load-versus-memory comparisons. The underlying component
labels are `maas` and `metering-sso`.

CPU and memory are resource measurements, not API-health checks. Sampling can miss
a brief memory spike before an OOM; use the saved failure state and telemetry gaps
as well as the charts. PriceTag database-pool and usage-ingestion metrics remain
in **Database & usage metering**.

### Outstanding generator requests

The Benchmark concurrency chart derives outstanding requests as dispatched minus
completed outcomes. This excludes completed tasks whose asynchronous cleanup
callbacks have not run yet. It includes requests still being validated by the
client; compare it with the gateway’s own active-request metric rather than
expecting the two boundaries to match exactly.

### Find a failed inference trace

Open **Inference traces → Failed inference traces**. This table filters sampled inference spans marked as errors and links to their span details. Check the HTTP status in the selected trace; span errors are not an HTTP 5xx counter. An empty table does not prove there were no failures. Use the gateway response metrics and the time-window links to sanitized Loki events alongside it.
