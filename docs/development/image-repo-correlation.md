# Image to repository correlation

Which git repository's code ships inside each container image this project
deploys or tracks, and which pipeline generates the image. Derived from image
labels (`org.opencontainers.image.source` / `.revision`), repository CI
workflows, Quay tag patterns and the EnMaaS-opps README; verified 2026-11-12.

## Gold-standard images (EnMaaS-opps deployments)

| Image | Code from | Built by | Used by |
| --- | --- | --- | --- |
| `quay.io/redhat-services-prod/enmaas-tenant/praxis-ai@sha256:bcc31cac…` (rev `d7ef08ce`, v2.43) | `redhat-et/praxis-ai` (via internal mirror; revision is an internal merge SHA) | Red Hat internal Konflux pipeline | EnMaaS-opps app-sre-prod/stage; **this repo's praxis pin since 2026-11-12** |
| `quay.io/redhat-services-prod/enmaas-tenant/metering-service@sha256:4c2d485a…` (rev `c0b2deac`, v2.43) | `redhat-et/pricetag-metering` (via internal mirror; public repo has no container workflow) | Konflux | EnMaaS-opps app-sre-prod/stage; this repo blocked (no standalone layer — see the [parity page](enmaas-opps-parity.md)) |
| `quay.io/modden/praxis-ai` (prod slots `87b85df8`/`7fa1fbb5`) | `redhat-et/praxis-ai` (its own `.github/workflows/publish-quay.yaml`, features `full,gcp-adc-filter`) | that workflow, on every push to `main` | EnMaaS-opps prod/stage/int slot pins |
| `quay.io/enmaas/maas-api@sha256:bbf5b77a…` (rev `c4ab05a`, single tag `prod-2026-10-17`) | `opendatahub-io/models-as-a-service` lineage (internal build; revision not public) | internal build + promotion | EnMaaS-opps app-sre maas pin |
| `quay.io/opendatahub/maas-api` (tags like `odh-pr-*`, version releases; cosign-signed) | `opendatahub-io/models-as-a-service` (`build-test.yml` PR builds, `create-release.yml` releases) | that repo's CI | public maas-api releases |
| `quay.io/opendatahub/praxis-experimental@sha256:502f120f…` (rev `5667c4ac`) | `praxis-proxy/experimental` (its `container.yaml`/`publish.yaml`) | that repo's CI | this repo's **previous** praxis pin (experimental lineage, replaced) |

## This deployment's images (built by this repository's CI)

| Image | Code from | Built by | Notes |
| --- | --- | --- | --- |
| `quay.io/redhat-et/secure-single-server-maas-standalone@sha256:0bec6ef5…` (rev `6ac4c80`) | `Ladas/models-as-a-service` branch `feat/standalone-runtime-minimal` | `redhat-et/enmaas-mini` CI: `.github/workflows/pricetag-images.yml` fetches the pinned fork revision, builds with `scripts/pricetag/build-maas` | current maas pin |
| `quay.io/redhat-et/secure-single-server-pricetag-metering@sha256:dabb2f35…` (rev `ce9c6c51`) | `redhat-et/pricetag-metering` branch `standalone-management` — redhat-et main plus the standalone management layer ([PR #149](https://github.com/redhat-et/pricetag-metering/pull/149)) | same workflow, via `scripts/pricetag/build-metering` and `configs/pricetag/Containerfile.metering` | current metering pin (predates the repoint — it is the earlier Ladas-fork build); the workflow and installer source constants now build `aefcacd` |
| `docker.io/library/postgres@sha256:996e9dc5…` | upstream PostgreSQL | Docker official images | metering/maas database |

## Not yet published

| Image | Code from | Built by | Status |
| --- | --- | --- | --- |
| (none) — future standalone maas image | `redhat-et/maas-api` (`MAAS_DEPLOYMENT_PROFILE=enmaas`) | nothing yet: no CI workflow; `Dockerfile` and `Dockerfile.konflux` are in tree | the official standalone maas home; needs a publish workflow (the `praxis-ai` `publish-quay.yaml` is the pattern) before this repo can switch its maas pin |
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
