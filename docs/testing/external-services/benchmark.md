# Sequential mock benchmark

**Status:** scheduler, HTTPS transport and failure-cleanup tests pass locally. The RHEL controller has not yet been qualified on the new lab. Do not interpret these tests as a capacity result.

## Run on the dedicated mock gateway

Install the gateway in benchmark/mock mode first. This command refuses a deployment with real provider routes or credentials. From its checkout on the gateway VM:

```sh
sudo python3 tests/external-services/benchmark.py --duration 600 --offered-rps 100
```

The six phases are baseline, 20 users, four streams per user, bursts, deliberate request-rate limiting and recovery. Each admits requests for ten minutes, followed by a bounded drain of requests already started. Fast 429 responses do not end the phase early. The default request deadline is 30 seconds; `--timeout` permits up to 60 seconds.

For a short installation check, use `--duration 10`. The burst duty cycle remains 30 seconds active/30 seconds idle, so a ten-second run does **not** qualify the idle part. `--offered-rps` controls the global admission target while active; concurrency and response time may prevent reaching it. The deliberately rate-limited phase must produce both successful inference and 429 responses or the run fails qualification.

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

The generator runs on the gateway host and consumes CPU/memory itself. Record this when comparing results; a separate generator is needed for cleaner capacity measurements. This runner does not yet emit phase markers to the remote collector or produce final resource graphs. Remote dashboards, retention deletion, service outages and full RHEL/RDS integration remain acceptance gates in the [lab design](design.md).

## Inspect the results

Use the [offline report instructions](report.md) to graph recorded CPU and memory alongside the workload outcomes.
