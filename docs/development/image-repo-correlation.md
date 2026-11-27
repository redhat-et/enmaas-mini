# Image to repository correlation

Which git repository's code ships inside each container image this project
deploys or tracks, and which pipeline generates the image. Derived from image
labels (`org.opencontainers.image.source` / `.revision`), repository CI
workflows, Quay tag patterns and the EnMaaS-opps README; verified 2026-11-26.

## Gold-standard images (EnMaaS-opps deployments)

| Image | Code from | Built by | Used by |
| --- | --- | --- | --- |
| `quay.io/redhat-services-prod/enmaas-tenant/praxis-ai@sha256:bcc31cac…` (rev `d7ef08ce`, v2.43) | `redhat-et/praxis-ai` (via internal mirror; revision is an internal merge SHA) | Red Hat internal Konflux pipeline | EnMaaS-opps app-sre-prod/stage; this repo's praxis pin until 2026-11-26 (replaced by the public mirror below) |
| `quay.io/redhat-services-prod/enmaas-tenant/metering-service@sha256:4c2d485a…` (rev `c0b2deac`, v2.43) | `redhat-et/pricetag-metering` (via internal mirror; public repo has no container workflow) | Konflux | EnMaaS-opps app-sre-prod/stage; this repo blocked (no standalone layer — see the [parity page](enmaas-opps-parity.md)) |
| `quay.io/modden/praxis-ai` (prod slots `87b85df8`/`7fa1fbb5`) | `redhat-et/praxis-ai` (its own `.github/workflows/publish-quay.yaml`, features `full,gcp-adc-filter`) | that workflow, on every push to `main` | EnMaaS-opps prod/stage/int slot pins |
| `quay.io/enmaas/maas-api@sha256:bbf5b77a…` (rev `c4ab05a`, single tag `prod-2026-10-17`) | `opendatahub-io/models-as-a-service` lineage (internal build; revision not public) | internal build + promotion | EnMaaS-opps app-sre maas pin |
| `quay.io/opendatahub/maas-api` (tags like `odh-pr-*`, version releases; cosign-signed) | `opendatahub-io/models-as-a-service` (`build-test.yml` PR builds, `create-release.yml` releases) | that repo's CI | public maas-api releases |
| `quay.io/opendatahub/praxis-experimental@sha256:502f120f…` (rev `5667c4ac`) | `praxis-proxy/experimental` (its `container.yaml`/`publish.yaml`) | that repo's CI | this repo's **previous** praxis pin (experimental lineage, replaced) |

## This deployment's images (published or built by this repository)

| Image | Code from | Built by | Notes |
| --- | --- | --- | --- |
| `quay.io/redhat-et/praxis-ai@sha256:240bcdaa…` (rev `548f61cc`) | `redhat-et/praxis-ai` (public mirror of the `quay.io/modden/praxis-ai` build; features `full,openai-all,store-postgres,gcp-adc-filter,vertex-anthropic-filter,api-key-auth-filter`) | the `redhat-et/praxis-ai` publish workflow, on push to `main` | this repo's praxis pin since 2026-11-26 |
| `quay.io/redhat-et/enmaas-api@sha256:fb8e02ab…` (rev `aea9c56`) | `redhat-et/enmaas-api` (private source; FastAPI/psycopg reimplementation of the maas-api key service) | that repository's own pipeline (not this repo) | this repo's maas pin since 2026-11-26; the installer runs `python -m app.migrate` before the service |
| `quay.io/redhat-et/secure-single-server-pricetag-metering@sha256:dabb2f35…` (rev `ce9c6c51`) | `redhat-et/pricetag-metering` branch `standalone-management` — redhat-et main plus the standalone management layer ([PR #149](https://github.com/redhat-et/pricetag-metering/pull/149)) | `redhat-et/enmaas-mini` CI: `.github/workflows/pricetag-images.yml` via `scripts/pricetag/build-metering` and `configs/pricetag/Containerfile.metering` | current metering pin (predates the repoint — it is the earlier Ladas-fork build); the workflow and installer source constants now build `e4ba826` |
| `registry.access.redhat.com/hi/postgresql@sha256:52e94353…` (`17.12-fips`, per-arch manifests; arm64 `sha256:f097ed32…`) | upstream PostgreSQL | Red Hat `hi/postgresql` (hummingbird) images; anonymous pull, no registry login | metering/maas database |

## Not yet published

| Image | Code from | Built by | Status |
| --- | --- | --- | --- |
| `quay.io/modden/enmaas-policy` | `redhat-et/enmaas-platform` (`apps/enmaas-policy`) | `redhat-et/enmaas-platform` CI (`publish-policy.yaml`), manual until the Quay repo exists | policy service for praxis; not used by this deployment |

## Verifying the correlation

Re-derive any row from the image itself:

```console
$ sudo podman image inspect IMAGE@sha256:… --format \
    'source={{index .Labels "org.opencontainers.image.source"}}
revision={{index .Labels "org.opencontainers.image.revision"}}'
```

The revision must match the pinned commit (the installer enforces this for
every image it deploys). For the Konflux-built `enmaas-tenant` images the
revisions are internal merge SHAs that do not exist in the public
repositories — the public repo remains the code source of truth, and the
image follows it on the next internal build cycle.
