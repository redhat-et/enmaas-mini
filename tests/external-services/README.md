# External-services scripts

Run from the repository root. These are testing tools; the application installer is [`scripts/pricetag/install.py`](../../scripts/pricetag/install.py). Start with the [lab guide](../../docs/testing/external-services/README.md).

## Infrastructure and trust — administrator workstation

| Entry point | Operation |
| --- | --- |
| `aws-discover` | Execute with Bash before deployment: hidden credential prompts, read-only account/DNS/subnet/VM discovery, nonsecret selection output. |
| `aws-lab.sh` | Source it for three-VM plan/deploy/info; per-launch confirmations remain required. |
| `network.py` | Review/apply narrow peer security-group grants using deployment journals. |
| `certificates.py` | Issue role-specific TLS bundles; signing keys stay on the workstation. |
| `rds.py` | Plan, explicitly provision, or verify optional private RDS. |
| `deployments.py` | Register/select local deployment metadata; render exact-principal trust, inspection and DNS policy proposals. No AWS calls. |
| `dns.py` | Plan, create, inspect or delete exact journaled records in an existing public zone; [DNS qualification](../../docs/testing/external-services/dns.md) remains pending. |
| `cleanup-inventory.py` | Read-only ownership/dependency audit; `--teardown-plan` adds ordered manual review with explicit retention decisions. No deletion. |

## RHEL nodes — generator then installer

| Node | Generate inputs | Install / operate |
| --- | --- | --- |
| Identity | `keycloak-host.py` | `install-keycloak` |
| Observability | `observability-host.py` | `install-observability` |
| Gateway telemetry | `telemetry-agent.py` | `install-telemetry-agent`; restricted `host-metrics.py` sampler |
| External database bootstrap | `rds-bootstrap.py` | Protected application-role creation; no business-data writes |

Generators take explicit arguments; installers consume reviewed directories and protected files. They do not create AWS resources. Commands, prerequisites and input locations are in the [headless guide](../../docs/testing/external-services/headless.md).

The auxiliary installers use `firewall-preflight.py` to check existing runtime
and permanent rules before creating secrets and starting services. Live RHEL
qualification remains required.

`deployments.py` is an initial **local inventory and DNS-policy generator** only.
It does not provision resources, create AWS credentials, connect to a host or
perform cleanup. Do not treat its manifests as live AWS ownership evidence.

## Local Podman fixture

| Entry point | Operation |
| --- | --- |
| `local.py` | Start/check/test/stop the named loopback-only identity/observability fixture. |
| `gateway-sso.py` | Add the rendered gateway, PriceTag Metering and optional MaaS/mock providers; test identity and inference boundaries. |
| `gateway-signals.py` | Add the gateway collector agent and verify emitted metrics/traces through mTLS. |
| `local-host-metrics.py` | Sample the local Linux host and send aggregate resources. |
| `collector-outage.py` | Bounded receiver outage: mock inference/accounting, recovery and cleanup; opt-in fixture interruption. |
| `profile-inference.py` | Exercise every realistic mock alias/native API and reconcile imported-price accounting. |
| `local-benchmark.py` | Run six mock phases and reconcile accounting; restores policies and retires test users/keys. |
| `report.py` | Render a private offline resource/traffic report. |

See [local setup](../../docs/testing/external-services/local-podman.md) and [dashboard access](../../docs/testing/external-services/local-dashboards.md). Select the same `ENMAAS_LAB_INSTANCE` for all commands. Never run fixture-management commands concurrently with a benchmark.

## Qualification tests and shared support

- **Isolated runtime suite:** `python3 tests/external-services/security-runtime.py` pulls pinned public images and checks private-issuer/OAuth2 Proxy authorization, issuer lockout/recovery and collector privacy/request sizes. CI runs the same command without AWS/provider credentials or the named demo fixture.
- **Unit contracts:** `test_*.py`, run with `python3 -m unittest discover -s tests/external-services -p 'test_*.py'`.
- **Identity/browser boundaries:** `browser.cjs`, `oidc-client.cjs`, `gateway-sso.cjs`, `gateway-lifecycle.cjs`, `group-revocation.cjs`, `keycloak-boundary.py`; `private-issuer-runtime.py` checks hostname TLS on an isolated internal network, and `--keycloak` adds login/PKCE/userinfo checks through `private-issuer-client.py`; `issuer-lockout.py` uses a separate disposable issuer for failed-login/temporary-lockout/recovery checks.
- **Telemetry/privacy/UI:** `collector-trust.py` (missing/unrelated client CA, wrong server CA and valid-client controls), `signals.py`, `redaction.py`, `gateway-dashboard.cjs`, `metering-dashboard.cjs`, `trace-dashboard.cjs`, `logs-dashboard.cjs`, `log-runtime.py`.
- **Linux/runtime checks:** `host-metrics-runtime.py`, `rds-bootstrap-runtime.py`.
- **RHEL benchmark:** `benchmark.py`; shared `scenarios.py`, `transport.py` and `traffic-patterns.json` define bounded phases and request behavior.
- **Private fixture bridges:** `gateway-identities.py`, `management-client.py`, `inference-probe.py` run inside the fixture; they are not public management services.
- **Pinned assets:** `images.json`, `*-vm.json` and `perses/`. Imported dashboard references have separate provenance; only explicitly tested dashboards are qualified.

The files remain in this directory to preserve imports and documented commands. This index separates operational entry points from test internals without introducing duplicate launchers.

The runtime suite allows 90 seconds for fixture cleanup after a test timeout or
interruption. A timeout still fails the suite. Forced termination, runner loss or
a failed cleanup operation can leave resources behind: discard ephemeral CI
runners, and inspect the test-specific containers/secrets on a local host before
reuse. No broad container or secret pruning is performed.
