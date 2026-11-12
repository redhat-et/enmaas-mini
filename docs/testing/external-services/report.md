# Benchmark report

Generate an offline report on your workstation from a local or copied RHEL benchmark directory. It must contain `summary.json`, `cleanup.json`, and either `resources.jsonl` (local) or `observe.jsonl` (RHEL).

From the repository checkout:

```sh
python3 -m venv .state/report-venv &&
.state/report-venv/bin/pip install -r tests/external-services/requirements-report.txt

REPORT_DIR=/absolute/path/to/benchmark-results
.state/report-venv/bin/python tests/external-services/report.py "$REPORT_DIR"
```

Open `report.html` in that directory. Keep `resources.png` beside it. Neither file requires a server or network connection. Review before sharing: the report excludes user keys and raw API responses, but workload results may still be operationally sensitive.

The report shows:

- Users, maximum requests in flight per user, phase duration, successes, rate-limit denials and achieved throughput.
- Container CPU and memory samples aligned with the workload phases.
- Whether ledger reconciliation and cleanup completed.

CPU is calculated from cumulative CPU-time differences between samples; it may exceed 100% on multiple cores. A new container needs two samples, and restarts break the series. Memory is container usage, not the host's available memory. Short phases and sparse samples cannot establish sustained capacity. The report does not infer percentile latency, retention behavior or production sizing.
