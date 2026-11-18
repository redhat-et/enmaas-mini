# Storage and retention

These are the generated **test deployment defaults**, not a production sizing
recommendation. Retention settings have been inspected; elapsed deletion and
full-disk recovery remain acceptance work.

## What grows and what expires

| Store | Current configuration | Operational limit |
| --- | --- | --- |
| Prometheus | 48 hours and 512 MB retention; persistent volume | The first retention threshold applies. This is not a filesystem quota: WAL, active chunks and compaction require additional space. |
| Loki | 24-hour global retention; TSDB index; compactor retention enabled; persistent volume | Deletion is asynchronous. No filesystem quota is configured. |
| Tempo | 24-hour block retention; local blocks and WAL on a persistent volume | Block retention is not a disk quota or immediate deletion guarantee. |
| Gateway service log files | Podman `k8s-file`, `max-size=10mb` per selected service | This bounds those files, not all journal/container logs or the application database. Rotation can lose unread events. |
| Collector | Bounded in-memory export queues; persistent log-reader positions on RHEL | Position checkpoints are not a durable export queue. Restart/outage can lose signals. |
| PriceTag and MaaS PostgreSQL | No history-purge job is installed by these helpers | Key revocation and identity retirement do not delete accounting/audit history. Review service-owned retention before adding any purge. |
| Keycloak PostgreSQL | Persistent identity database on RHEL | Retiring inference keys does not remove issuer accounts. Bulk test-user cleanup is separate. |
| Images, build cache and local evidence | Retained after tests | No scheduled pruning. Review deployment ownership before removal; never use a global prune as deployment cleanup. |

The application database remains the accounting authority. Removing telemetry
must not delete financial history. Refer to the upstream behavior for
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/),
[Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)
and [Tempo configuration](https://grafana.com/docs/tempo/latest/configuration/#compaction).

## Observe a run

The [benchmark report](report.md) graphs host free space, database and WAL size
where available. Keep samples before, during and after traffic. Retain the
benchmark timestamps so dashboard queries use the right interval.

Do not infer retention success from a stable filesystem: compaction, unrelated
activity and reuse of allocated database pages can hide growth or deletion.
Do not infer deletion from an empty dashboard query alone.

## Acceptance still required

On disposable storage with the pinned images:

1. Ingest identifiable synthetic signals and record their timestamps, query
   results and storage sizes. Keep recent control data as well.
2. Wait beyond the configured retention window and each store's deletion cycle.
   Verify expired data disappears while recent data remains; inspect storage and
   service deletion/error metrics. Do not shorten retention on shared demo data.
3. Restart each store and collector; verify retained data and resumed ingestion.
   Record any signal loss separately from inference/accounting correctness.
4. Exercise low-space behavior on an isolated test filesystem. Verify denial,
   alerts and recovery without filling the gateway or workstation root disk.
5. Verify backup/restore and retained-volume cleanup independently. Time-based
   telemetry retention does not establish either capability.

Until these checks pass, keep storage growth visible and retain sufficient free
space. Use the [cleanup inventory](cleanup.md) for deployment-owned volumes,
RDS backups and local evidence; stopping a service does not remove its data.
