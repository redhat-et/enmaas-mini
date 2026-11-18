# Benchmark report

Generate an offline report on your workstation from a local or copied RHEL benchmark directory. It must contain `summary.json`, `cleanup.json`, and either `resources.jsonl` (local) or `observe.jsonl` (RHEL).

## Retrieve a completed RHEL run

First check the benchmark unit and `cleanup.json` on the gateway. Do not copy its
whole result directory: it also contains test keys and saved configuration.

From the workstation installation shell, restore `RHEL_HOST` and `SSH_OPTS` for
the **gateway**. Replace `DEPLOYMENT_ID` with the installed checkout's ID and
`RUN_ID` with the run ID printed by the benchmark. For a manually installed
checkout, use its actual path instead of `/opt/enmaas-mini/DEPLOYMENT_ID`.

```sh
(
  set -e
  umask 077
  set -C
  ssh "${SSH_OPTS[@]}" "$RHEL_HOST" \
    'sudo -n python3 /opt/enmaas-mini/DEPLOYMENT_ID/tests/external-services/report-bundle.py export /root/pricetag-admin/scenario-perf-RUN_ID' \
    > benchmark-results.tar
  python3 tests/external-services/report-bundle.py import \
    benchmark-results.tar .state/benchmark-results
)
```

Run this from the workstation checkout. Existing archives and destination
directories are refused; choose new names for another run. A failed SSH export
can leave a partial archive: inspect/remove that file before retrying. The helper
exports only `summary.json`, `cleanup.json`, and one resource-sample file. It
rejects links, oversized files and unexpected/duplicate archive members.

An imported report is not a passing test: check both accounting and cleanup
results. These files still contain operational measurements and test identity IDs;
the helper is not a general-purpose secret redactor. Review before sharing.

## Render graphs

From the repository checkout:

```sh
python3 -m venv .state/report-venv &&
.state/report-venv/bin/pip install -r tests/external-services/requirements-report.txt

REPORT_DIR=/absolute/path/to/benchmark-results
.state/report-venv/bin/python tests/external-services/report.py "$REPORT_DIR"
```

Open `report.html` in that directory. Keep `resources.png` and, when generated, `host-storage.png` beside it. Neither file requires a server or network connection. Review before sharing: the report excludes user keys and raw API responses, but workload results may still be operationally sensitive.

The report shows:

- Users, configured concurrency (per-user or a shared cap), phase duration, successes, rate-limit denials and achieved throughput.
- Container CPU and memory samples aligned with the workload phases.
- For a local sustained run verified with `monitoring-result.py`, or an async staircase verified with `stress-result.py`: a per-phase headroom table from `capacity-summary.json`, including host CPU, available memory/disk, sampled container memory totals, database-pool waiting and mean inference duration.
- When the RHEL samples contain them: available/cached host memory, free filesystem
  percentage, selected PostgreSQL database size and cluster WAL directory size.
- Whether ledger reconciliation and cleanup completed.

Service CPU is calculated from cumulative CPU-time differences and displayed in cores: one fully busy CPU is one core. Host CPU percentages separately use the whole host as 100%. A new container needs two samples, and restarts break the series. Memory is container usage, not the host's available memory. Short phases and sparse samples cannot establish sustained capacity. The report does not infer percentile latency, retention behavior or production sizing.

Available and cached memory overlap; do not add them together. Missing storage
observations appear as gaps, and unavailable series (for example external-DB WAL
size) are omitted. Database/WAL sizes include activity beyond this test and do not
establish that retention deletes old data or that backups are recoverable.

See [storage and retention](storage.md) before interpreting growth as a retention result.
