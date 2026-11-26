# Profile database work during a local benchmark

An optional diagnostic for the disposable Podman fixture. It compares PostgreSQL
statement counters before and after each streaming stage. It does not connect to
RDS, change application data or enable extensions automatically.

## Prerequisites

Use the [local benchmark](local-podman.md) with `pg_stat_statements` already
preloaded and its extension installed in the fixture's `metering_sso` database.
The default fixture does **not** enable this extension. Enabling it requires a
reviewed PostgreSQL configuration change and restart; do that before collecting
a run, not during load. Preserve any existing preload libraries.

Confirm the selected fixture and inspect readiness on your workstation:

```bash
export ENMAAS_LAB_INSTANCE=YOUR_EXISTING_LOCAL_FIXTURE
POD="enmaas-$ENMAAS_LAB_INSTANCE"
test "$(podman pod inspect "$POD" --format '{{index .Labels "io.enmaas.test-lab"}}')" = "$ENMAAS_LAB_INSTANCE" &&
podman exec "$POD-db" psql -X -U keycloak -d metering_sso \
  -c 'SHOW shared_preload_libraries;' \
  -c 'SELECT stats_reset, dealloc FROM pg_stat_statements_info;'
```

Both commands must succeed and the preload list must contain `pg_stat_statements`.
Do not replace a database, drop its data or disable a failing check to satisfy
this prerequisite. The profiler uses the local fixture's existing administrative
socket access; it is not a least-privilege remote database exporter.

## Collect and render

Add `--database-profile` to the reviewed `local-stress.py` command from the
[streaming benchmark guide](benchmark.md). Keep its provider image, rate,
concurrency, phase duration and resource guards explicit. The profiler's database
work is additional load and should be included when comparing runs.

Set `RUN_DIR` to the private report path printed by that run:

```bash
.state/local-venv/bin/python tests/external-services/database-report.py "$RUN_DIR"
```

This writes a private `database-analysis.md` beside the stage files. Review it
before sharing; do not copy the rest of the state directory or database logs.

## Interpret the measurements

- **Calls and aggregate execution time:** successful statement executions during
  the stage, including background work. Concurrent execution times can sum to
  more than wall-clock time; this is not CPU time or request latency.
- **Weighted mean:** aggregate execution time divided by calls. The report also
  lists rare expensive statements separately from the largest total consumers.
- **Maximum:** since the statistics reset, **not** a stage maximum or percentile.
- **Buffer reads:** PostgreSQL buffer misses, not necessarily physical disk I/O.
- **Categories:** heuristics such as usage reads/writes and pricing reads, not
  proven API endpoints. A query fingerprint identifies a statement shape within
  this database; it is not a globally stable identifier.

Raw SQL, bind parameters, user identities and credentials are not exported.
Snapshots allow only numeric counters, query fingerprints and fixed categories.
More than 1,000 statement shapes, resets, evictions or decreasing counters reject
interval comparison. The profiler does not measure connection-pool waiting,
failed-query latency or reconcile asynchronous database calls with requests.
