# EnMaaS-opps image and configuration parity

EnMaaS-opps (`redhat-et/EnMaaS-opps`) is the operations repository for the
production EnMaaS platform: OpenShift templates, per-environment parameters,
secrets and ConfigMaps for the same service set this repository deploys on a
single RHEL host. This page tracks the effort to converge this deployment onto
the images and configuration EnMaaS-opps runs in production ("gold standard"),
one image at a time.

## Image roadmap

EnMaaS-opps pins digest-identified images per environment. The
`quay.io/redhat-services-prod/enmaas-tenant/*` mirrors are the set used by its
app-sre-prod and app-sre-stage environments; they require Quay credentials with
`redhat-services-prod` pull access (`sudo podman login quay.io` before install).

| Component | This repository (previous) | EnMaaS-opps app-sre pin | Status |
| --- | --- | --- | --- |
| Praxis gateway | `quay.io/opendatahub/praxis-experimental@sha256:502f120f…` (praxis-proxy/experimental line, rev `5667c4ac`) | `quay.io/redhat-services-prod/enmaas-tenant/praxis-ai@sha256:bcc31cac…` (redhat-et/praxis-ai line, rev `d7ef08ce`) | Switched |
| Metering | `quay.io/redhat-et/secure-single-server-pricetag-metering@sha256:dabb2f35…` | `quay.io/redhat-services-prod/enmaas-tenant/metering-service@sha256:4c2d485a…` | Blocked — needs the standalone layer upstreamed (see below) |
| MaaS | `quay.io/redhat-et/secure-single-server-maas-standalone@sha256:0bec6ef5…` | `quay.io/enmaas/maas-api@sha256:bbf5b77a…` | Blocked — no standalone mode upstream (see below) |
| PostgreSQL | `docker.io/library/postgres@sha256:996e9dc5…` | CNPG cluster / RDS (Kubernetes-specific) | Not applicable |

The `quay.io/modden/*` images used by the non-AppSRE EnMaaS-opps environments
are private to that namespace; the `enmaas-tenant` mirrors build from the same
`redhat-et/praxis-ai` source line and carry the same filter-config schema.

## Why metering and MaaS cannot swap yet

The praxis switch was a pin swap because praxis is a config-driven proxy with
no Kubernetes dependency. The metering and MaaS images are different: the
EnMaaS builds come from the upstream sources, which assume Kubernetes (CRD
catalog, ConfigMap pipeline config, partner/session admin flows), while this
repository's pins are **standalone forks** carrying small patch sets:

| Component | EnMaaS image source | Standalone fork | Fork-only patches |
| --- | --- | --- | --- |
| Metering | `redhat-et/pricetag-metering` (upstream main has no standalone layer as of 2026-11-12) | `Ladas/pricetag-metering` branch `feat/latest-upstream-standalone` | `/api/v1/management/*` (users, budgets, prices, keys), `GATEWAY_MODELS_FILE`, `MANAGEMENT_API_SECRET`/`MANAGEMENT_PUBLIC_ORIGIN`, `USER_DASHBOARDS_ENABLED` |
| MaaS | `opendatahub-io/models-as-a-service` (no `MAAS_RUNTIME_MODE` upstream) | `Ladas/models-as-a-service` branch `feat/standalone-runtime-minimal` | `MAAS_RUNTIME_MODE=standalone`, `MAAS_SUBSCRIPTION_MODE=standalone` (run without Kubernetes) |

### Metering qualification against the EnMaaS tenant image

The tenant metering image
(`enmaas-tenant/metering-service@sha256:4c2d485a…`, upstream rev `c0b2deac`,
v2.43) was qualified on a database clone on 2026-11-12 (never the only copy,
per the upgrade guide):

- Container mechanics are identical: same `/metering-service` entrypoint, uid
  1001, port 8080; runs standalone with the K8s adapter gracefully disabled.
- **Schema-compatible**: migrations complete with no changes (16 tables before
  and after); the fork's database works as-is.
- Health, readiness and the dashboard login all serve (200).
- It self-seeds pricing (253 models fetched, GE commercial discount policy) —
  the installer's price bootstrap would not be needed.
- The m2m events contract (`/api/v1/events`) is unchanged.

What is missing, confirmed against the running image:

- `/api/v1/management/{users,budgets,prices,keys,…}` → 404. The installer's
  bootstrap (sample users, USD budgets) and the `manage` CLI depend on these.
- Partner `/api/v1/users` exists (covers user creation and per-user keys via
  `USER_MANAGEMENT_API_SECRET`) but no enmaas-mini equivalent is configured.
- Per-user USD budgets have **no HTTP surface at all** in upstream — the
  storage functions (`UpsertQuotaOverride`) exist, but only the fork's
  management layer calls them. Upstream meters with a global token quota plus
  QoS tiers instead.
- `GATEWAY_MODELS_FILE` (file-based catalog) is not read; the catalog comes
  from Kubernetes CRDs.

### Path to convergence

Upstream the standalone patch sets (the forks exist specifically to carry
them), then switch the pins once EnMaaS builds include them:

1. Metering: port `Ladas/pricetag-metering:feat/latest-upstream-standalone`
   (management layer, file catalog) to `redhat-et/pricetag-metering`, from
   where Konflux builds the tenant image.
2. MaaS: the official standalone home already exists — `redhat-et/maas-api`
   with its `MAAS_DEPLOYMENT_PROFILE=enmaas` profile. What is missing is a
   published image (no CI yet, though a `Dockerfile.konflux` is in tree).
   Switching this repository to it additionally requires quadlet changes:
   the profile uses `MAAS_DEPLOYMENT_PROFILE=enmaas` where the generated
   unit sets `MAAS_RUNTIME_MODE=standalone`, and the supported environment
   for the EnMaaS key-management API (database URL file, key expiry, admin
   groups, trusted proxies) must be qualified against the profile.
3. Re-run the qualification above against the resulting builds and swap the
   pins; the installer and manage CLI then work unchanged for metering, and
   with the quadlet env changes for maas.

The metering storage layer is shared between the lines, so its patches are
additive (new handlers and env flags) rather than divergent.

### Repository lineage

The EnMaaS project's repositories in `github.com/redhat-et` (verified
2026-11-12), with the external origins and standalone forks:

| Repository | Role |
| --- | --- |
| `redhat-et/praxis-ai` | AI gateway (GitHub fork of `praxis-proxy/ai`); builds `quay.io/modden/praxis-ai` via its own `publish-quay.yaml`, and Konflux builds the `enmaas-tenant/praxis-ai` image from its internal mirror |
| `redhat-et/pricetag-metering` | Metering service and dashboard; Konflux builds `enmaas-tenant/metering-service` from its internal mirror (no container workflow in the public repo) |
| `redhat-et/maas-api` | **New (2026-11-05): dedicated public repo for the standalone maas-api used by EnMaaS** — extracted from the `maas-api/` subtree of `opendatahub-io/models-as-a-service` with history preserved, based on `yossiovadia/models-as-a-service:feat/standalone-api-key-mode`; adds the `MAAS_DEPLOYMENT_PROFILE=enmaas` profile (with `MAAS_SUBSCRIPTION_MODE=standalone`), hermetic vendored builds and a `Dockerfile.konflux`. No CI workflow or published image yet. |
| `redhat-et/enmaas-platform` | Services monorepo: `apps/enmaas-policy` (stateless authorization and QoS policy service for praxis; publishes `quay.io/modden/enmaas-policy`), with `apps/enmaas-api` planned as a `git subtree` import from `redhat-et/enmaas-api` |
| `redhat-et/enmaas-api` | Key-validation API service deployed in AppSRE (GitHub fork of `redhat-et/turnstile`) |
| `redhat-et/EnMaaS-opps` | Deployment and operations repository (the gold standard) |
| `redhat-et/enmaas-mini` | This repository — the single-host standalone deployment |
| `redhat-et/pricetag` | PriceTag enterprise control-plane deployment and operations home (OpenShift) |
| `redhat-et/secure-single-server` | Sibling single-server project (OpenCode/OpenClaw harness); the origin of the `secure-single-server-*` Quay image namespace this repository's CI publishes into |

External origins and standalone forks:

| Component | Origin | Standalone fork (this repo's pins) | New official standalone home |
| --- | --- | --- | --- |
| Praxis | `praxis-proxy/ai` (plus the separate `praxis-proxy/experimental` repo behind the previous pin) | none needed — switched to the EnMaaS build | — |
| Metering | `vishnoianil/pricetag-metering` (dormant since 2026-09-30) | `Ladas/pricetag-metering:feat/latest-upstream-standalone` (actively maintained) | none yet — the management layer exists only in the fork |
| MaaS | `opendatahub-io/models-as-a-service` | `Ladas/models-as-a-service:feat/standalone-runtime-minimal` (builds on `yossiovadia`'s `feat/standalone-api-key-mode-v2`) | `redhat-et/maas-api` |

### Image build pipelines

The full image-to-repository correlation — which git repository's code ships
inside each image, and which pipeline builds it — lives in the
[image to repository correlation](image-repo-correlation.md) page. In short:
Konflux builds the `enmaas-tenant` app-sre images from internal mirrors of
`redhat-et/praxis-ai` and `redhat-et/pricetag-metering`; `redhat-et/praxis-ai`
additionally publishes `quay.io/modden/praxis-ai` from its own workflow; and
this repository's own `pricetag-images.yml` builds the standalone pins from
the Ladas fork revisions pinned in that workflow.

Consequences for making changes: the Konflux-built `enmaas-tenant` images
follow the public repositories through the internal mirror, so code changes
land via pull requests to `redhat-et/pricetag-metering` and
`opendatahub-io/models-as-a-service` with no build-configuration work on our
side (digest updates for the app-sre deployments flow through the internal
app-interface saas file, as EnMaaS-opps documents). This repository's own
image workflow is the second direct lever: it already builds standalone
images from pinned fork revisions and can be repointed at new sources.

Notes for the metering port: the EnMaaS images' revision labels (for example
`c0b2deac` for metering, `d7ef08ce` for praxis, `c4ab05a` for maas-api) are
internal CI merge commits that do not exist in the public repositories — the
images build from an internal mirror of these lines, so merged code reaches
the images on the next build cycle. The Ladas fork also carries the
vishnoianil-line versions of upstream pull requests under different SHAs, so
the port is a rebase of the standalone commits onto `redhat-et` main (202
commits ahead, including its own versions of the same pull requests); expect
conflict resolution in the administration UI areas, while the storage layer
is identical.




### Switch validation checklist

Validated for the praxis switch on a fresh AWS RHEL 9 host (2026-11-12):

- Private pull with Quay credentials, revision-label check passes
  (`org.opencontainers.image.revision` = `d7ef08cea44bf111…`, version 2.43).
- Image runs as uid 1001 with `ENTRYPOINT ["/usr/local/bin/praxis-ai"]`; the
  generated quadlet's `User=1001`/`UserNS=keep-id` and
  `Exec=-c /etc/praxis/gateway.json` work unchanged.
- The generated `gateway.json` loads on the new binary without config errors:
  `router`, `model_access`, `api_key_auth`, `external_metering`, `token_count`,
  `headers` and `load_balancer` are all still registered with the fields this
  renderer emits.
- End-to-end: user key auth, model access, upstream TLS to `api.openai.com`,
  chat completion (200), and metering with cost in `usage_events`.
- Provider-secret rotation (`providers openai --replace`) preserves users,
  budgets, usage and prices.

Known difference: the `enmaas-tenant` build is compiled without the `otel`
feature, so the generated telemetry settings produce a startup warning and
OTLP export is disabled (the experimental image enabled `otel`).

## Secrets

What EnMaaS-opps provisions as Kubernetes secrets, mapped to this deployment:

| EnMaaS-opps secret | Purpose | Here | Gap |
| --- | --- | --- | --- |
| `provider-credentials` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CB_LITELLM_API_KEY`) | Upstream keys, injected as env, stripped from client requests by `credential_injection` | Per-profile Podman secrets (`praxis-openai-api-key-v1`, `praxis-external-api-key-v1`) via quadlet `Secret=` | Equivalent; a consolidated multi-key secret would match the ops shape |
| `metering-internal-auth` (`token`) | Praxis → metering internal auth | `pricetag-m2m` Podman secret | Equivalent |
| `postgresql-credentials` (`METERING_DB_URL`) | Metering database URL | Internal PostgreSQL, or external URL file flags | Equivalent |
| `pricetag-session` (`SESSION_SECRET`) | Dashboard session signing | Generated at install | Equivalent |
| `metering-user-management-api`, `metering-partner-api` | Management/partner API auth | `management-secret` | Subset; partner APIs are not exposed here |
| `metering-readonly-db-url` (`READ_DATABASE_URL`) | Read replica for dashboards | None | Optional future feature |
| `vertex-sa-key` (`sa-key.json`) | GCP service account for the `gcp_adc` filter (Vertex models) | None | Needed only when Vertex upstreams are added |
| `cb-litellm-qos-keys` | QoS key aliases for weighted-token classification | None | Needed only when QoS is adopted |

## ConfigMaps and filter-chain deltas

EnMaaS-opps serves `praxis.yaml` plus hot-reloaded routing overlays from
ConfigMaps. This repository generates an equivalent `gateway.json` at install
time from saved provider inputs. Structural differences worth converging on:

| EnMaaS-opps behavior | Here today | Notes |
| --- | --- | --- |
| Routing overlays (`intelligent_route` + `model_catalog` with `overlay_file`), hot-reloaded without restart | Static `model_access` allowlists + `router` routes generated at install | Biggest gap. Overlays allow model/cluster changes without gateway restarts; the running build supports both styles |
| `api_key_auth` with `cache_ttl_seconds: 300` | `cache_ttl_seconds: 0` | Every request currently revalidates against MaaS; ops caches validation for 5 minutes |
| `external_metering` with `fail_open: true` and `model_policy_fail_open: true` | `fail_open: false` | Ops treats metering outages as non-blocking (routing still bounds reachable models); here an outage blocks inference |
| Public `GET /health` via `static_response` before auth | None (health only on the loopback admin listener) | Load-balancer/uptime probes need an unauthenticated health path |
| `reject_upgrade` (websocket) guard | None | Connection upgrades can tunnel past streaming metering |
| `token_count` with `max_scratch_bytes: 1048576` | Default | Large Responses-API `response.completed` events exceeded the default and under-metered Codex usage in prod (incident 2026-09-17) |
| Upstream clusters with `idle_timeout_ms: 45000` | Default | Stale-connection guard for long-lived upstream pools |
| `request_remove: accept-encoding` | Not removed | Compressed upstream responses can break usage scanning |
| `stream_usage_inject`, `time_to_first_token`, `token_usage_headers` | Not emitted | Usage/observability headers and TTFT metrics |
| Container `ulimit -n 65536`, readiness probe on `/healthy`, replicas with `maxUnavailable: 0` | Single container, systemd restart | fd exhaustion at high concurrency surfaces as upstream 502s |

The QoS subsystem (`praxis-qos-prod` ConfigMap, `QOS_*` env, weighted-token
classification with premium/main/ratelimit tiers) is a larger feature without a
current counterpart here; it depends on the metering image switch first.
