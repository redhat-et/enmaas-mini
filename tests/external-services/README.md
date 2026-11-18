# External-services scripts

Run from the repository root. These are testing tools; the application installer is [`scripts/pricetag/install.py`](../../scripts/pricetag/install.py). Start with the [lab guide](../../docs/testing/external-services/README.md).

## Infrastructure and trust — administrator workstation

| Entry point | Operation |
| --- | --- |
| `aws-discover` | Execute with Bash before deployment: hidden credential prompts, read-only account/DNS/subnet/VM discovery, nonsecret selection output. |
| `aws-lab.sh` | Source it for three-VM plan/deploy/info; per-launch confirmations remain required. |
| `infrastructure.py` | Preview/create a dedicated network and three restricted hosts; preserves network and VM journals. Does not yet configure peers, DNS, RDS or services. |
| `infrastructure-info.py` | Verify live host ownership/ingress and print addresses; optionally apply explicitly confirmed private-peer rules or journaled DNS records, then write protected service configuration. Application readiness remains separate. |
| `vpc-plan.py` | Preview a dedicated network; optional read-only account/zone/CIDR preflight. |
| `vpc-create.py` | Offline plan by default; explicit confirmed creation with durable journals. Recovery/teardown and live qualification remain pending. |
| `vpc-inspect.py` | Offline journal inspection: confirmed resources versus uncertain operations. No AWS calls or deletion authorization. |
| `network-policy.py` | Render a group-scoped network IAM fragment for review. No role creation; live IAM qualification remains required. |
| `vm-policy.py` | Render VM provisioning grants for explicit AMIs, instance types and disk cap. No role creation. |
| `policy-pack.py` | Split reviewed fragments into size-bounded managed-policy documents without changing grants. No AWS calls or policy validation. |
| `operator-profile.py` | Preview/add nonsecret role-profile settings; preserve existing config and refuse profile overwrite. No AWS calls or credential-file edits. |
| `operator-bootstrap.py` | Preview/create a group-scoped network/VM role, with optional zone-scoped DNS grants, explicit admin profile, confirmation and journal. RDS/cleanup grants and live IAM qualification remain pending. |
| `network.py` | Review/apply narrow peer security-group grants using deployment journals. |
| `services.py` | Offline plan and journaled SSH execution of the existing component installers on fresh RHEL hosts, including exact-commit source checkout. All-host preflight, strict host trust and no replay of uncertain steps. |
| `transfer-client.py` | Transfer fixed issuer client files, credential-free population exports or allowlisted role TLS bundles over verified SSH. Refuses existing destinations; no private values printed or CA signing keys transferred. |
| `certificates.py` | Issue role-specific TLS bundles; signing keys stay on the workstation. |
| `rds.py` | Plan, explicitly provision, or verify optional private RDS. |
| `deployments.py` | Register/select local deployment metadata; render exact-principal trust, inspection and DNS policy proposals. No AWS calls. |
| `dns.py` | Plan, create, inspect or delete exact journaled records in an existing public zone; [DNS qualification](../../docs/testing/external-services/dns.md) remains pending. |
| `cleanup-instances.py` | VM-only termination prototype: read-only preview, explicit confirmation, repeated ownership checks and durable intent. No retries or other resource deletion; live IAM/teardown qualification pending. |
| `cleanup-inventory.py` | Read-only ownership/dependency audit; `--teardown-plan` adds ordered manual review with explicit retention decisions. No deletion. |
| `cleanup-groups.py` | Read-only post-termination group plan; exact ownership checks and interface/foreign-reference blockers. No deletion. |

## Synthetic-user lifecycle — issuer and gateway hosts

Start with [synthetic users](../../docs/testing/external-services/keycloak-users.md).
The issuer retains passwords; only whitelisted identity profiles cross to the gateway.

| Entry point | Operation |
| --- | --- |
| `user-population.py` | Population state machine with private per-user records and safe resume. |
| `populate-keycloak.py` | Issuer-host command using loopback TLS and the test bootstrap administrator; issuer runtime and separate 10,000-user creation verified; complete RHEL/gateway-population scale pending. |
| `keycloak-password.py` | On the issuer host, explicitly retrieve one saved persona or numbered population-user password; service and bootstrap credentials cannot be selected. |
| `import-population.py` | Gateway-host partner API import and retirement of issuer UUIDs; receipts prevent uncertain-write replay. Retirement revokes partner-minted keys only. `--configure-access` sets finite keys and budgets; persona roles use `personas.py`. RHEL qualification remains pending. |
| `population-report.py` | Offline CPU/memory chart from disposable issuer resource samples; distinguishes interval CPU from lifetime averages. |
| `persona-runtime.py` | Fresh local databases and temporary services: actual persona API setup, no-write resume and role drift denial; removes only owned fixtures. |
| `population-key.py` | Select one population inference key; metadata by default, explicit export only after private receipt and current API checks. |
| `personas.py` | Import issuer UUIDs, configure Super admin/Alice/Bob roles and team membership, verify budgets; refuse collisions and uncertain privilege writes. |
| `zone-select.py` | Read-only discovery UI: validate public-zone metadata, select a zone through the terminal and print shell-safe nonsecret exports. No AWS calls or files written. |
| `population-runtime.py` | Local labelled-pod API probe (`--users 2–10000`): identity/budget/key setup, selected-key mock inference and scoped usage, verified HTTPS login, role denial, team assignment, retirement and complete key revocation. |

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
Its `access` command binds a saved service configuration to the deployment ID
and SSH key before printing URLs and selected credential commands. It does not
provision resources, create AWS credentials, connect to a host or perform cleanup. Do not treat its manifests as live AWS ownership evidence.

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
| `report-bundle.py` | Export/import exactly three measurement files; excludes keys/configuration, refuses links and existing destinations. |

See [local setup](../../docs/testing/external-services/local-podman.md) and [dashboard access](../../docs/testing/external-services/local-dashboards.md). Select the same `ENMAAS_LAB_INSTANCE` for all commands. Never run fixture-management commands concurrently with a benchmark.

## Qualification tests and shared support

- **Isolated runtime suite:** `python3 tests/external-services/security-runtime.py` pulls pinned public images and checks private-issuer/OAuth2 Proxy authorization, issuer lockout/recovery and collector privacy/request sizes. CI runs the same command without AWS/provider credentials or the named demo fixture.
- **Unit contracts:** `unit/test_*.py`, run with `python3 -m unittest discover -s tests/external-services -p 'test_*.py'`.
- **Identity/browser boundaries:** `browser.cjs`, `oidc-client.cjs`, `gateway-sso.cjs`, `gateway-lifecycle.cjs`, `group-revocation.cjs`, `keycloak-boundary.py`; `private-issuer-runtime.py` checks hostname TLS on an isolated internal network, and `--keycloak` adds login/PKCE/userinfo checks through `private-issuer-client.py`; `issuer-lockout.py` uses a separate disposable issuer for failed-login/temporary-lockout/recovery checks.
- **Secret-file runtime:** `transfer-runtime.py` runs the unchanged remote read/write scripts as root and a non-root user in disposable, networkless containers. It verifies private ownership, no overwrite, symlink refusal and root-only access; no host files or real secrets are mounted.
- **Issuer route probe:** `issuer-routes-runtime.py` starts a disposable pinned Keycloak and generated Praxis proxy with no published ports. It checks discovery before/after admin, master-realm and encoded-path probes, and verifies that forged Host/forwarded headers cannot change discovery issuer/endpoint URLs. `--praxis-image` supports an explicit native local override; that result does not qualify the default deployment image.
- **Telemetry/privacy/UI:** `collector-trust.py` (missing/unrelated client CA, wrong server CA and valid-client controls), `signals.py`, `redaction.py`, `gateway-dashboard.cjs`, `metering-dashboard.cjs`, `trace-dashboard.cjs`, `logs-dashboard.cjs`, `log-runtime.py`.
- **Linux/runtime checks:** `host-metrics-runtime.py`, `rds-bootstrap-runtime.py`.
- **RHEL benchmark:** `benchmark.py`; shared `scenarios.py`, `transport.py` and `traffic-patterns.json` define bounded phases and request behavior.
- **Private fixture bridges:** `gateway-identities.py`, `management-client.py`, `inference-probe.py` run inside the fixture; they are not public management services.
- **Image platform preflight:** `image-platforms.py` inspects every auxiliary digest and requires native Linux amd64/arm64 index entries before the security suite starts. It performs registry reads only and rejects platform-specific image locks.
- **Pinned assets:** `images.json`, `*-vm.json` and `perses/`. Imported dashboard references have separate provenance; only explicitly tested dashboards are qualified.

## Source layout

- `unit/`: isolated unit contracts, discovered recursively by the command above.
- `perses/`: maintained dashboard templates and datasource definitions.
- `monitoring.py`: generated dashboard and alert-rule definitions; the local and RHEL configuration generators render these into their protected output directories.
- Top-level commands: infrastructure, service setup, benchmark and runtime probes; existing CLI paths remain stable.

To inspect generated dashboard JSON and alert rules without starting services:

```sh
python3 tests/external-services/monitoring.py --output .state/monitoring-preview
```

Edit `monitoring.py` for generated dashboards, or the maintained templates under `perses/`. Generated output belongs in `.state/`, not in source control.

The runtime suite allows 90 seconds for fixture cleanup after a test timeout or
interruption. A timeout still fails the suite. Forced termination, runner loss or
a failed cleanup operation can leave resources behind: discard ephemeral CI
runners, and inspect the test-specific containers/secrets on a local host before
reuse. No broad container or secret pruning is performed.

## AWS and installer coverage

See [the mocked coverage suite](../aws/README.md) for CI scope, dummy credentials,
branch reports and the remaining work toward 100% coverage. A passing unit suite
does not establish live AWS permissions or complete RHEL installation acceptance.

`cleanup-keys.py` previews exact imported EC2 key-pair removal after verified VM
termination. Its separate apply mode retains local keys and journals; IAM grants
and live AWS qualification remain pending. See [cleanup](../../docs/testing/external-services/cleanup.md).

### Browsable benchmark team

`alice-team.cjs` explicitly attaches a completed 25-profile local run to the
existing Alice SSO identity: 15 people and 10 bots. It uses authenticated public
APIs, verifies exact team usage and budgets, checks cross-team/admin denials, and
captures the rendered My team page. See [local dashboard instructions](../../docs/testing/external-services/local-dashboards.md#browse-alices-team).
