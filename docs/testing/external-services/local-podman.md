# Local stack qualification

Run the gateway, PriceTag Metering, MaaS, Keycloak and observability services in
an isolated Podman pod. All published ports bind to loopback. Test users,
credentials and LLM responses are synthetic; no cloud account is needed.

This tests application contracts before the [three-host RHEL installation](three-host-install.md).
It does not qualify RHEL firewall/SELinux, remote routing, public access or capacity.

## Prepare

Use a running Linux Podman engine (a Podman machine on macOS), Python, Node.js,
Git and OpenSSL. Do not stop a machine that holds another deployment.
Run these commands from the clone.

```bash
python3 -m venv .state/local-venv
.state/local-venv/bin/pip install 'PyYAML==6.0.3' -r tests/external-services/stress-requirements.txt
npm ci --prefix tests/pricetag/browser --ignore-scripts
tests/pricetag/browser/node_modules/.bin/playwright install chromium
```

On Linux, install Playwright's system dependencies with
`tests/pricetag/browser/node_modules/.bin/playwright install-deps chromium`.

Build the reviewed application candidates once. Auxiliary images and Praxis
are digest-pinned in `tests/external-services/images.json`. Published PriceTag
images are separate from the source candidate needed for these OIDC checks.

```bash
mkdir -p .state/sources
git init .state/sources/metering &&
git -C .state/sources/metering fetch --depth 1 \
  https://github.com/Ladas/pricetag-metering.git 2281e1a84bfc3b998e7db36bc01bae88130bfcdd &&
git -C .state/sources/metering checkout --detach FETCH_HEAD &&
.state/local-venv/bin/python scripts/pricetag/build-metering --source .state/sources/metering \
  --revision 2281e1a84bfc3b998e7db36bc01bae88130bfcdd \
  --tag localhost/pricetag-metering:upstream-2281e1a &&
git init .state/sources/maas &&
git -C .state/sources/maas fetch --depth 1 \
  https://github.com/Ladas/models-as-a-service.git 6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d &&
git -C .state/sources/maas checkout --detach FETCH_HEAD &&
scripts/pricetag/build-maas --source .state/sources/maas \
  --expected-commit 6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d
```

## Run the complete checks

```bash
ENMAAS_LAB_INSTANCE=external-ci \
  .state/local-venv/bin/python tests/external-services/local-qualification.py
```

The runner refuses existing state, pods or retained data volumes, runs browser/API checks and removes
only its labelled containers and secrets. Private evidence remains in
`.state/external-ci/`; named data volumes remain for inspection. Never upload
the whole state directory: it contains test credentials and the private CA. For another
local run without deleting evidence, select a fresh `external-ci-` name followed
by 8–12 hexadecimal characters. Both qualification runners accept this form.
Use an unused `ENMAAS_LAB_PORT_OFFSET` and check all four resulting ports against
other fixtures; different offsets can still overlap between service ports.

Checks cover verified TLS and telemetry redaction; browser SSO, group and
current-role enforcement; user provisioning, budgets, finite keys and retirement;
JSON/SSE inference with exact token/cost reconciliation; and gateway, Linux
host, container and database telemetry.

The **Local stack qualification** workflow runs contract tests on PRs. Its full
runtime job is manually dispatched because it builds both application images.
CI uses a confined root-owned Podman store; the RHEL service deployment remains
rootless. A green contract job alone does not establish runtime qualification.

## Keep a fixture open for inspection

Choose an unused instance name. The optional port offset shifts all four
loopback ports together, including OIDC redirects; retain the same values for
every subsequent command. Offset `12000` uses 30443, 31443, 36443 and 40443
instead of 18443, 19443, 24443 and 28443.

```bash
export ENMAAS_LAB_INSTANCE=local-review
export ENMAAS_LAB_PORT_OFFSET=12000
.state/local-venv/bin/python tests/external-services/local.py start --gateway &&
.state/local-venv/bin/python tests/external-services/local.py test &&
.state/local-venv/bin/python tests/external-services/gateway-sso.py start \
  --metering-image localhost/pricetag-metering:upstream-2281e1a \
  --maas-image localhost/pricetag-maas:standalone --mock-profiles \
  --praxis-image quay.io/opendatahub/praxis-experimental@sha256:502f120fed7b8e516ce1d78396045c8f72673672351432b9dc2e61baf5c13ac8 &&
.state/local-venv/bin/python tests/external-services/gateway-sso.py test
```

Endpoints and the CA path print at startup; exact URLs are also stored in
`.state/local-review/endpoints.json`. Passwords are in the protected
`credentials.json` beside it. Only the issuer's `admin` account belongs to the
observability group. Gateway roles are assigned separately; SSO group claims
do not confer gateway administrator access. Tests deliberately deactivate Bob.

For local group-revocation testing, add `--authorization-test` at **fresh**
fixture creation. That adds a scoped realm-management client; it is never
enabled by the RHEL issuer installer.

Enable gateway telemetry once, then keep the sampler running while observing:

```bash
.state/local-venv/bin/python tests/external-services/gateway-signals.py \
  --praxis-image quay.io/opendatahub/praxis-experimental@sha256:502f120fed7b8e516ce1d78396045c8f72673672351432b9dc2e61baf5c13ac8 &&
.state/local-venv/bin/python tests/external-services/local-host-metrics.py --continuous
```

Host metrics describe the **Linux Podman host**, not the macOS host. Component
metrics describe containers in the selected pod. This acceptance run is not a
sustained benchmark; idle panels can legitimately have no traffic.

Stop the sampler with Ctrl-C, then remove only this fixture's containers/secrets:

```bash
.state/local-venv/bin/python tests/external-services/local.py down
```

Keep `.state/local-review/` and its named volumes together if retaining evidence.
Do not reuse old databases with freshly generated credentials. No command here
prunes the engine or removes another deployment.

## Run a mock benchmark

Keep the fixture above running and enable its gateway telemetry first. The workload
mixes the configured mock providers, human and bot profiles, and API clients.
Each phase admits requests for the selected duration, then drains outstanding work.

```bash
.state/local-venv/bin/python tests/external-services/local-benchmark.py \
  --praxis-image quay.io/opendatahub/praxis-experimental@sha256:502f120fed7b8e516ce1d78396045c8f72673672351432b9dc2e61baf5c13ac8 \
  --scenario sustained --duration 600
```

Use `--duration 65` for a shorter acceptance run. The final output prints the
protected `RUN_DIR`; use that value to validate the run and generate its report:

```bash
.state/local-venv/bin/python tests/external-services/monitoring-result.py "$RUN_DIR"
```

For the complete fresh-fixture benchmark, use this **instead of** the service-only
qualification command. It refuses existing `external-ci` state, validates streaming,
accounting and browser data, then removes its containers and secrets. After the
benchmark retires its callers, the runner explicitly prepares Alice's 25-member
synthetic display team with the [team helper](local-dashboards.md). It verifies
all inference keys remain revoked/expired before checking active-user charts.
This affects only the disposable fixture; retained evidence volumes are private.

```bash
ENMAAS_LAB_INSTANCE=external-ci \
  .state/local-venv/bin/python tests/external-services/ci-runtime.py --duration 65
```

The **Local stack qualification** workflow offers the same `benchmark` option;
choose 65 or 600 seconds per sustained phase. Its small streaming staircase is an
acceptance test, not a maximum-throughput claim. Read the [scenario and capacity
controls](benchmark.md), [dashboard guide](local-dashboards.md), and [report guide](report.md).
