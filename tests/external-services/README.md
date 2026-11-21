# External identity and observability tests

The issuer slice provides a shared realm generator, protected RHEL Quadlet
inputs, a dedicated-host installer and isolated security probes.

[RHEL setup and boundaries](../../docs/testing/external-services/keycloak-rhel.md)

Run unit contracts without cloud credentials or containers:

```console
python3 -m unittest discover -s tests/external-services/unit -v
bash -n tests/external-services/install-keycloak
shellcheck -x tests/external-services/install-keycloak
```

With a running local Podman engine, run independent temporary fixtures:

```console
python3 tests/external-services/issuer-routes-runtime.py
python3 tests/external-services/issuer-lockout.py
```

The route test publishes no host ports and checks canonical issuer metadata,
forged forwarding headers, forbidden administration/master-realm paths and
encoded traversal. The lockout test publishes a random loopback port, verifies
successful login, rejection during temporary lockout and recovery. Allow roughly
two minutes for the real configured lockout period.

Both use synthetic credentials and remove their own test containers/secrets.
They do not touch the running gateway or benchmark. CI executes unit and runtime
checks with pinned images; local rootless Podman and CI's sudo Podman execution
are separate evidence from the RHEL Quadlet installation.

`realm.py` contains configuration only; it must not import monitoring, local
deployment or benchmark modules. `images.json` records the issuer's pinned
images. The role certificate generator also prepares future gateway and
telemetry bundles, with separate HTTPS and client-authentication trust.

## Observability transport

[RHEL observability and gateway relay](../../docs/testing/external-services/observability-rhel.md)

The shared `service_config.py` renderer does not import a benchmark runner.
It generates store configurations, the transport-test dashboard and operational
panels/alerts. Benchmark dashboards remain separate. See the
[monitoring guide](../../docs/testing/external-services/monitoring.md) for source
coverage, interpretation and browser/query checks.

With the pinned images from `images.json` and the Python image from
`scripts/pricetag/prepare.py` available in Podman:

```console
python3 tests/external-services/private-issuer-runtime.py --keycloak
python3 tests/external-services/redaction.py
python3 tests/external-services/test-alert-rules.py
python3 tests/external-services/telemetry-runtime.py
```

The first probe checks private issuer resolution, real login/PKCE and OAuth2
Proxy group enforcement. The second checks client-certificate rejection, plain
and compressed payload limits, and privacy for logs, traces and five metric
kinds. The rule test checks deterministic alert timing and query semantics. The
transport test provisions Perses and checks every operational query against
known signals in Prometheus, Loki and Tempo. Only the redaction probe publishes a random loopback port;
other fixtures have no published ports. They use generated test credentials
and clean up only their own temporary resources.

Add `--browser` to the telemetry probe to check all operational dashboards
with Playwright. Rootless runs publish only an ephemeral loopback Perses port.
Rootful Linux CI uses `nsenter` to run the browser in the disposable pod's network
namespace: the network stays internal and no host ports are published. This is
a test-runner operation; deployed services remain rootless. Browser failures
retain synthetic-only screenshots and a Playwright trace for diagnosis.

The `external-observability.yml` workflow runs these probes on Linux, using
native-architecture pinned images and no cloud credentials. They do not install
services or certificates on the host. RHEL installation requires separate
SELinux, firewall, restart, storage and network qualification.

## Coordinate prepared RHEL hosts

Use the [three-host installation guide](../../docs/testing/external-services/three-host-install.md)
for an offline plan, verified SSH transfers, per-stage receipts and access commands.
It creates no AWS resources and refuses existing deployments.

## AWS infrastructure

Start with [scoped operator access](../../docs/testing/external-services/operator-access.md),
then [dedicated infrastructure](../../docs/testing/external-services/infrastructure.md).
[Cleanup](../../docs/testing/external-services/cleanup.md) lists owned resources and
which deletion operations remain manual. Live IAM and RHEL qualification is pending.
