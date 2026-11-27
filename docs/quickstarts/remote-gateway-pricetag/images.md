# Application images

The installer defaults to the previous qualified, digest-pinned release.
Use source mode for the latest metering candidate before publication. Praxis,
MaaS and PostgreSQL remain pinned published images in either mode; only the
PriceTag metering image has a source-build mode.

| Mode | MaaS and PriceTag |
| --- | --- |
| `--image-mode published` (default) | Pull the pinned MaaS and PriceTag demo images. No application source checkout. |
| `--image-mode source` | Build PriceTag from a branch or commit on RHEL to test a new version; MaaS is still the published `enmaas-api` image. |

The installer checks image source labels and CPU architecture. MaaS is the
published `quay.io/redhat-et/enmaas-api` image (built from the private
`redhat-et/enmaas-api` source); it is never built from source here.

## Open the installation session

For a **fresh host**, run from the workstation installation shell:

```console
ssh -t "${SSH_OPTS[@]}" "$RHEL_HOST" \
  'cd ~/secure-single-server && exec bash'
```

Run examples below in **this SSH session**. New shell? Restore
[connection settings](README.md#1-set-the-connection) first.

## Published images

Publication repositories:

```text
quay.io/redhat-et/praxis-ai
quay.io/redhat-et/enmaas-api
quay.io/redhat-et/secure-single-server-pricetag-metering
```

The published images target `linux/amd64`. They are pinned in
`scripts/pricetag/install.py`; no moving `latest` tag is used. Override them only
to test another publication.

<details markdown="1">
<summary>Override published MaaS and PriceTag images</summary>

```console
cd ~/secure-single-server
read -r -p 'Gateway IP or DNS: ' GATEWAY_HOST
read -r -p 'MaaS repository@sha256:digest: ' MAAS_IMAGE
read -r -p 'PriceTag repository@sha256:digest: ' METERING_IMAGE
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --image-mode published --maas-image "$MAAS_IMAGE" --metering-image "$METERING_IMAGE"
```

Provider selection and hidden credential prompts follow. For unattended
configuration, combine these options with the [private credential-file
options](providers.md). Never put provider keys in command-line arguments.

</details>

<details markdown="1">
<summary>Build from the latest metering branch</summary>

Source mode defaults to the
[PriceTag standalone-management branch](https://github.com/redhat-et/pricetag-metering/tree/standalone-management).

The candidate reuses upstream **My usage** and **API keys** at `/account`, with
the standalone administration and authorization changes. The previous published
image has **My keys** at `/management`. The branch resolves to a full commit
before building, recorded in the image label and checked on import.

For the exact reviewed candidate, use
`--metering-revision e4ba826d5b1469d7f1167428792e498ffb4d92c7`
with source mode. Branch mode may include later commits.

```console
cd ~/secure-single-server
read -r -p 'Gateway IP or DNS: ' GATEWAY_HOST
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" --image-mode source
```

</details>

<details markdown="1">
<summary>Build another metering branch or commit</summary>

For branches, add `--metering-branch BRANCH` to source mode. For pinned commits,
use the block below. Source: [PriceTag metering](https://github.com/redhat-et/pricetag-metering).

```console
cd ~/secure-single-server
read -r -p 'Gateway IP or DNS: ' GATEWAY_HOST
read -r -p 'PriceTag source commit: ' METERING_REVISION
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --image-mode source --metering-revision "$METERING_REVISION"
```

Use a branch **or** a full SHA, never both. Branches resolve to a printed commit
before building. PriceTag schema/API changes need compatibility testing.

</details>

<details markdown="1">
<summary>Image verification and upgrade limits</summary>

Revision options also set expected source labels for published images. A mismatch
stops installation. Labels are provenance metadata, not signatures; use trusted
digests. These commands install fresh hosts, not upgrades or database migrations.

</details>
