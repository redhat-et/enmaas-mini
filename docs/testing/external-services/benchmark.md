# Sequential mock benchmark

**Status:** local tests and a short six-phase run on the existing RHEL benchmark host pass, including exact event/token/USD reconciliation and cleanup. The sustained RHEL run and new three-host lab remain under qualification; the short run is not a capacity result.

## Run on the dedicated mock gateway

Install the gateway in benchmark/mock mode first. This command refuses a deployment with real provider routes or credentials. From its checkout on the gateway VM:

```sh
sudo python3 tests/external-services/benchmark.py --duration 600 --offered-rps 100
```

For a deployment running against an upgrade-clone database, append
`--database-name ACTUAL_DATABASE_NAME`. The default is `metering`. Use the active
metering database name: pricing checks, resource sampling and ledger reconciliation
all use that selection. This changes neither the service connection nor its data.

The six phases are baseline, 20 users, four streams per user, bursts, deliberate request-rate limiting and recovery. Each admits requests for ten minutes, followed by a bounded drain of requests already started. Fast 429 responses do not end the phase early. The default request deadline is 30 seconds; `--timeout` permits up to 60 seconds.

For the sustained ramp used by the [main monitoring scenario](monitoring.md), select the same shared plan explicitly:

```sh
sudo python3 tests/external-services/benchmark.py --scenario sustained --duration 600 --cycles 1
```

Its six phases set their own offered rates; `--offered-rps` is a fallback for phases without a configured rate. `--cycles` repeats the sequence, with a 20-hour total scheduled-load limit. Local phase markers and component dashboards are qualified separately; this does not establish remote sampling or RHEL acceptance.

For a short installation check, use `--duration 10`. The burst duty cycle remains 30 seconds active/30 seconds idle, so a ten-second run does **not** qualify the idle part. `--offered-rps` controls the global admission target while active; concurrency and response time may prevent reaching it. The deliberately rate-limited phase must produce both successful inference and 429 responses or the run fails qualification.

## Run independently of the SSH connection

On the mock gateway, use the same runner with `--detach`:

```sh
sudo python3 tests/external-services/benchmark.py --detach \
  --duration 600 --offered-rps 100
```

The command checks the mock-only configuration and submits a unique systemd unit.
The workload checks again while holding the provider lock. Submission is not a
passing benchmark: use the printed `systemctl status` and `journalctl` commands to
follow it. The journal includes the private result-directory path; the result
files and keys stay on the gateway. Do not publish whole logs/results directories.

Successful completion remains visible as `active (exited)` with exit status zero;
a failed run is `failed`. Verify `summary.json` and `cleanup.json` as well. The
unit does not restart automatically or survive a host reboot. A submission timeout
may mean the job started: inspect the printed unit before submitting another run.

Use the printed `systemctl stop` command for graceful cancellation. The main
runner receives SIGTERM and attempts restoration/key retirement, with up to 180
seconds before systemd kills remaining processes. Forced termination, power loss
or a cleanup failure still requires manual investigation. Stop the completed unit
when its status is no longer needed; this does not remove the private report.

Detached submission and safe preflight failure were also tested on RHEL: a legacy
mock deployment without an explicit model price was refused before traffic or
account provisioning. Its cleanup report completed successfully. A subsequent short six-phase RHEL run passed with the active database selected.
Sustained load and reboot/failure recovery remain separate qualification gates.

A price-preflight failure writes `failure_stage: mock_price_preflight` to
`summary.json`. Check database access and that the selected mock model has exactly
one explicit, nonnegative price before retrying. The runner does not invent a
rate, bypass accounting or restart the gateway for this failed preflight.

## Behavior and evidence

- Takes the existing provider-configuration lock and rechecks the mock-only deployment after acquiring it.
- Creates dedicated `perf-*` users with synthetic USD allowances. Existing user keys are not reused.
- Applies each scenario's gateway rate/burst values, preserving the original configuration privately for recovery.
- Uses verified HTTPS, refuses redirects, validates mock JSON/SSE usage and bounds headers/body time and response size. Every request uses a new TLS connection; this cost is included in the measurement.
- Records fixed-size latency histograms, first-response-byte histograms, status counts, per-user completions and phase timestamps. First byte is **not** a guaranteed first generated token; do not label it TTFT.
- Samples RHEL CPU/memory/disk, service/container state and PostgreSQL counters. Stops on low available memory, low disk, service failure/restart or a request/protocol error.
- Snapshots the selected model's explicit price before traffic and calculates the mock's two input/three output token cost, rounded per event to eight USD decimal places. Missing/ambiguous prices stop the run before provisioning users.
- Reconciles per-user ledger event/token totals, non-null cost counts and **exact USD totals**. A price/discount change during the run can cause a deliberate reconciliation failure; the runner does not change pricing policy to make the test pass. Historical repricing and fault/replay qualification remain separate tests.
- Restores the original rate limit, suspends the run's users and revokes their keys on success or failure. Partial provisioning is included. Interrupted workloads drain before policy restoration. Provisioning/reconciliation subprocesses have bounded lifetimes and cancellation terminates their private process groups.

The command prints a private result directory under `/root/pricetag-admin`. It contains `summary.json`, `observe.jsonl`, `reconciliation.log`, `cleanup.json`, test key files and the original gateway configuration. **Do not publish that directory wholesale.** Keys and configuration are private; review/redact the selected measurements before sharing.

A failed `cleanup.json` needs investigation before another run. If forcibly killed or the host loses power, automatic cleanup cannot run: preserve `gateway-original.json` and the run prefix for manual restoration and test-account retirement. Do not overwrite the live configuration blindly if another administrator changed it after the failure.

## Remaining qualification

The generator runs on the gateway host and consumes CPU/memory itself. Record this when comparing results; a separate generator is needed for cleaner capacity measurements. The runner does not yet emit phase markers to the remote collector. Generate resource and storage graphs afterward with the [offline report](report.md). Remote dashboards, retention deletion, service outages and full RHEL/RDS integration remain acceptance gates in the [lab design](design.md).

## Inspect the results

Use the [offline report instructions](report.md) to graph recorded CPU and memory alongside the workload outcomes.

### Gradual streaming capacity run

For a finer-grained capacity experiment, add `--profile gradual --keep-gateway-running` to `local-stress.py`. This prepares a fixed population of 2,500 identities, then offers **10, 50, 100 and 150 requests/s**, followed by recovery at 10 requests/s. The default stage admission window is ten minutes (`--duration 600`); outstanding 10–120-second replies drain before the next stage.

The gateway rate policy remains at 1,000 requests/s, so expected policy rejection does not obscure the capacity experiment. `--keep-gateway-running` avoids restarting the gateway between stages only when its generated configuration and memory limit are unchanged. Recovery/restoration still restores the original configuration. Generator caps and resource safety checks remain enforced; missed admissions do not count as achieved gateway throughput.

The default `decade` profile retains the 10× offered-rate staircase. Use measured results, including missed admissions and exact accounting reconciliation, when comparing profiles.

### Read the capacity dashboard

The **Benchmark results → Mock provider performance** section separates backend
CPU cores and memory from gateway resource use. The memory chart includes the
mock container's configured limit. A mock OOM is a backend constraint, not a
measured gateway capacity limit; confirm OOMs with kernel/container evidence.
Missing metric samples alone do not prove a crash.

People and bot charts are alternative workload assumptions: one request/minute
per person or ten requests/minute per bot. Do not add those estimates or treat
them as tested seat counts. For a mixed team, calculate
`(people × requests/person/minute + bots × requests/bot/minute) / 60`.

At steady state, concurrent requests are approximately achieved requests/second
multiplied by mean end-to-end duration. The mock's configured 10–120-second
schedule is not measured model execution time. Client first-byte timing is not
necessarily first-token timing, and subtracting unrelated client, gateway or
backend averages does not isolate gateway overhead. Inspect failures alongside
latency: fast failures can lower the mean while service quality worsens.

### Varied users and budget exhaustion

Use `--scenario budgets` with `local-benchmark.py` (local Podman) or
`benchmark.py` (the dedicated mock RHEL gateway). This separate dashboard scenario
creates 15 people and 10 bots with varied request sizes, model choices and budgets.
Most people have $25/$100/$300 allowances; bots have $150/$500/$1,000. Two people
and two bots have $0.01 allowances to exercise exhaustion through real inference
and metering. It does not insert synthetic spending into the database.

The report distinguishes each user's successful requests, budget denials and
rate-limit denials. It passes only if both a person and a bot receive budget
rejections and successful requests reconcile with the ledger. Existing enforcement
and post-budget exceptions remain unchanged: disabled enforcement, free models or
an applicable exception can prevent exhaustion, and the scenario must not claim a
pass in that case. Increase the duration if the low-budget users have not yet spent
their allowance. Test identities are retired after the run; usage history remains.

`--budget-mix` enables these allowances for another scenario. Do not use it for
capacity qualification: budget rejection is an intentional policy outcome.

### Capacity above 10,000 concurrent requests

The async local runner supports `--profile extended --concurrency 40000`:
100 → 150 → 200 → 250 requests/second, then 10 requests/second recovery.
Use `--duration 600` for ten-minute admission stages, followed by draining requests.
The isolated mock backend permits a 40,000-request concurrency ceiling. This is
a configured bound, not measured sustainable capacity. The generator default
remains 5,000; the higher ceiling is an explicit experiment setting.

Responses stream for 10–120 seconds. At the approximate 65-second mean, sustained
250 requests/second implies about 16,250 active requests if all are admitted.
These are estimates, not capacity claims. Generator percentage means active
requests divided by its configured ceiling; host CPU and memory are separate.
Resource safety checks and the stop-on-failure/recovery behavior remain enabled.

For a targeted probe, add `--min-rate 250 --max-rate 250` to the extended
profile. This retains the ten-requests/second recovery stage. Raising concurrency
does not raise offered requests/second or the gateway rate limit. Check
`skipped_concurrency` and `skipped_scheduler` separately: zero concurrency skips
means the configured cap did not restrict admission. The runner requires a process
file-descriptor limit of at least concurrency plus 256; memory and disk guards stay
active. Rebuild the isolated mock image after changing its capacity bound.

The isolated mock also exposes authenticated, counts-only diagnostics at
`/__enmaas/capacity` on its internal listener: configured maximum, current and peak
active requests, and capacity rejections. This is a test-backend endpoint, not a
new public gateway route. Use it to distinguish mock-backend rejection from
gateway failure; a higher generator ceiling alone cannot establish the cause.

`--profile full` includes 10 → 50 → 100 → 150 → 200 → 250 requests/second,
then recovery. `--gateway-memory-mib 16384` is an explicit local experiment
allocation and is restored afterward. Record allocation changes alongside the
results; they must not be mistaken for application-only performance improvements.

For hosts with sufficient spare resources, `--profile higher` offers
500 → 750 → 1,000 requests/second, then recovery. Start with
`--min-rate 500 --max-rate 500` to measure one ten-minute stage before escalating.
The 40,000-request concurrency cap still applies: at a mean response duration of
65 seconds, 500 requests/second requires roughly 32,500 concurrent requests;
750 requests/second would exceed that cap. Skipped admissions identify a generator
limit, not a gateway failure. Inspect achieved throughput before increasing load.

Multiple generator processes sharing one source address and destination can still
exhaust the same ephemeral TCP-port pool. A connection error such as
`EADDRNOTAVAIL` in the client is a generator/network constraint, not an HTTP
rejection from the gateway. The same error on a gateway-to-listener connection
can instead produce an HTTP 502. Check which process and connection failed before
attributing the result to gateway capacity. More workers alone do not provide more source addresses; use
separate generator hosts or explicitly qualified network/source-address isolation
for larger experiments. Different source addresses within the application network
namespace can still interfere with its internal connections. Prefer separate
generator hosts or network namespaces, and record the forwarding path used. The
current CLI does not configure that isolation.

`--gateway-memory-mib 32768` is available for an explicitly sized local experiment;
it does not increase VM RAM. Keep headroom for the database, mock backend and other
services, and account separately for the host-side generator. Record any limit
change when comparing runs. Resource safety checks and cleanup remain enabled.
