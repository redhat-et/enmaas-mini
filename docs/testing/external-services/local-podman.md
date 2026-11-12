# Local Podman qualification

Test the external identity and observability services before deploying the three-VM lab. No AWS credentials, DNS changes or real provider keys are needed.

**Scope:** this fixture tests Keycloak → OAuth2 Proxy → Perses and mTLS collector → Prometheus/Loki/Tempo. It also tests the gateway OIDC client against Keycloak: signed immutable identity claims, issuer/audience/nonce, expiry and authorization-code replay rejection. The optional gateway tests below also exercise actual PriceTag login, account deactivation, shared SSO and Praxis metrics/traces. Native MaaS JSON/SSE inference, per-user accounting and all six traffic phases have passed locally. This is not a fresh RHEL installation test.

For interactive access after setup, follow [local dashboards](local-dashboards.md). See [topology](topology.md) for the two collector roles and [security acceptance](security-acceptance.md) for what this fixture cannot prove.

## Start and test

Use a running rootless Podman environment, Python 3 with PyYAML, OpenSSL and Node.js. The local test was run on an ARM64 Podman VM with 4 CPUs and 8 GiB RAM. Allow space for eight images and test data. Run from the repository checkout:

```sh
# macOS only, if the Podman machine is stopped:
podman machine start

npm ci --prefix tests/pricetag/browser --ignore-scripts
./tests/pricetag/browser/node_modules/.bin/playwright install chromium

python3 tests/external-services/local.py start &&
python3 tests/external-services/local.py test &&
python3 tests/external-services/redaction.py
```

The launcher uses the reviewed digests in `tests/external-services/images.json`. The first run creates random test credentials and seven-day certificates under `.state/external-services-local` (directory mode `0700`, files `0600`, ignored by Git). Credentials are mounted as Podman secrets, not passed in command arguments. The CA private key stays on the workstation. No trust store is changed.

| Endpoint | Purpose | Access |
| --- | --- | --- |
| `https://localhost:18443` | Keycloak realm `enmaas-lab` | Private-CA TLS; test identities only. |
| `https://localhost:19443` | Perses through OAuth2 Proxy | Only the `observability-admin` group. |
| `https://localhost:24443/v1/…` | OTLP HTTP ingestion | Private-CA TLS **and a client certificate**. |

All three ports bind to host loopback. During the issuer test, a temporary HTTPS callback receiver binds to `127.0.0.1:28443`; this is a test receiver, not a running Mini gateway. Database, Prometheus, Loki, Tempo and direct Perses ports are not published. Containers share one trusted pod network to give the browser and OIDC clients the same `localhost` issuer. This does **not** test isolation between the RHEL VMs.

## Repeat collector trust checks

The standard `local.py test` includes this probe. To repeat it independently against
the selected running fixture:

```sh
python3 tests/external-services/collector-trust.py
```

It verifies valid-client delivery before and after negative tests, rejects a
missing client certificate and a valid client chain signed by an unrelated CA,
and verifies that an unrelated server CA cannot establish trust. Temporary keys
are created privately and removed automatically. Existing service certificates
and trust stores are unchanged. This does not qualify expiry, revocation or
certificate rotation on separate RHEL nodes.

## Receiver outage and recovery

Run only against the existing **mock** gateway fixture with telemetry enabled.
This test takes the benchmark lock, temporarily stops its labeled collector,
creates a temporary ordinary user/key and restores the collector and gateway
configuration in cleanup. Do not run another benchmark or manage this fixture
concurrently.

```sh
python3 tests/external-services/collector-outage.py --seconds 30
```

The test checks successful inference and exact event/token/USD accounting while
the receiver is stopped, then fresh synthetic metrics/logs/traces after recovery. An additional inference
request must produce fresh gateway metrics and traces without replacing the
gateway container; its accounting is reconciled too.
It retires its temporary user/key and saves private evidence under the printed
run directory. Ctrl-C or SIGTERM requests restoration and cleanup; allow it to
finish. SIGKILL, host shutdown or an unavailable Podman engine can prevent cleanup:
inspect the selected fixture and its private run journal before another test. A 30-second local outage passed. This does **not** establish
lossless delivery of outage-time telemetry, exporter queue capacity, long-outage
behavior or separate-host availability.

## Private issuer hostname transport

```sh
python3 tests/external-services/private-issuer-runtime.py
python3 tests/external-services/private-issuer-runtime.py --keycloak
```

This disposable test uses an internal Podman network with no published ports. A
client resolves the issuer hostname to the server's private address, verifies its
CA/hostname and reads an issuer document. A wrong hostname is rejected. The test
removes its containers, secrets, network and signing keys; it does not change the
running demo or host DNS. It uses the pinned Python image already imported by the
lab and refuses implicit pulls.

The second command uses the pinned Keycloak image and a disposable development
database. It verifies that discovery, authorization, token, JWKS and logout URLs
retain the canonical DNS issuer when reached over the private address. It then
performs form login, PKCE code exchange and authenticated userinfo for the three
generated users with both clients. Immutable identity and observability group
claims must match; reused codes and wrong PKCE verifiers must fail. Both images
must already be present. It does not use the running demo realm.

The same test starts the pinned OAuth2 Proxy with the generated client settings,
private issuer mapping and a static protected response. It requires an accepted
administrator session, denied Alice/Bob sessions, and rejection of anonymous or
forged identity headers. This also requires the pinned auth-proxy image locally.
Only startup readiness is retried; authorization failures stop the test.

This qualifies issuer transport and the proxy's OIDC/session/group behavior,
**not browser cookie policies**, PriceTag sessions, three-host routing, public DNS
or RHEL firewall policy. The proxy uses its normal token validation; the separate
Python issuer-protocol client is not an application JWT verifier.
Passwords and client secrets are generated for this disposable realm, supplied
through Podman secrets to the fixture containers that need them, and never printed.

## What the test verifies

- Every service passes readiness, with server certificate/hostname verification.
- Anonymous observability requests fail; Super admin can open Perses; Alice and Bob are denied. The separate group-revocation test below checks denial of an already-issued session after membership removal.
- A forged OIDC callback cannot create a session. The pinned proxy currently returns HTTP 500 for the malformed callback; the test also requires the resulting session to remain unauthenticated.
- The collector rejects requests without a client certificate and accepts the issued test certificate.
- Synthetic metrics, logs and traces reach their respective stores and can be queried privately. A seeded `authorization` attribute is removed from logs and spans.
- The provisioned **Local transport test** dashboard opens in the browser. Its values are synthetic transport checks, not gateway usage.

The browser test trusts only the generated certificate's public key for its isolated Chromium process. Node and Python use the generated CA with normal verification. It saves a screenshot under `.state/external-services-local/perses-admin.png`; no test credentials are printed.

For interactive inspection, use `https://localhost:19443` after trusting this disposable CA in your test browser. The certificate is `.state/external-services-local/ca.pem`; test passwords are in `.state/external-services-local/credentials.json`. Do not reuse these identities or certificates outside the lab.

## Repeat and stop

```sh
python3 tests/external-services/local.py test
python3 tests/external-services/local.py down
```

`down` removes only the labelled lab pod and its labelled secrets. It keeps the private state and named database/backend volumes so `start` can resume. It does not prune other Podman resources. To fully reset this disposable fixture, stop it first, remove the following named volumes, then delete its local state:

```sh
python3 tests/external-services/local.py down &&
podman volume rm enmaas-external-local-db enmaas-external-local-perses \
  enmaas-external-local-prometheus enmaas-external-local-loki enmaas-external-local-tempo &&
rm -r .state/external-services-local
```

Reset after certificate expiry. A configuration change also requires recreating the fixture; restarting alone deliberately preserves its credentials and configuration.

## Before the RHEL test

This test does not establish production readiness. Remaining checks are:

- Use the pinned PriceTag source revision below. Existing-session role changes and confirmed logout passed with that revision; qualify the same image on RHEL.
- Repeat host-resource timer execution, restricted log forwarding and trace navigation on RHEL, and run the full ten-minute benchmark phases. These paths have local coverage with shorter traffic phases.
- Separate service certificates and private networks; systemd/Quadlet, SELinux, reboot and firewall behavior.
- Retention deletion and disk limits: Prometheus is configured for 48 hours/512 MB, Loki/Tempo for 24 hours; elapsed-time deletion is not yet qualified. Logs/traces have no tested disk quota.
- ARM64 was exercised locally; the RHEL AMD64 deployment still needs qualification.

Use synthetic telemetry only. The current attribute filtering is a test of the mechanism, **not a complete production redaction policy**. The test superadmin group is managed in Keycloak; it has not yet been linked to a PriceTag role.

Sources: [Keycloak production configuration](https://www.keycloak.org/server/configuration-production), [OAuth2 Proxy Keycloak provider](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/keycloak_oidc/), [Tempo 3.1.1 monolithic example](https://github.com/grafana/tempo/blob/v3.1.1/example/docker-compose/single-binary/tempo.yaml).

## Real gateway and PriceTag OIDC boundary

Use a fresh named fixture so an older realm/database cannot silently change the result. Only one fixture can occupy the fixed loopback ports at a time; stop the previous one with its matching `ENMAAS_LAB_INSTANCE` and `local.py down`. That command retains its volumes and private files.

```sh
export ENMAAS_LAB_INSTANCE=external-sso
python3 tests/external-services/local.py start --gateway
python3 tests/external-services/gateway-sso.py start \
  --metering-image "$METERING_IMAGE" --praxis-image "$PRAXIS_IMAGE"
python3 tests/external-services/gateway-sso.py test
```

Install the existing browser dependencies first (`npm ci --prefix tests/pricetag/browser` and its documented Playwright browser setup). Use the native-architecture PriceTag source candidate `2281e1a84bfc3b998e7db36bc01bae88130bfcdd` and Praxis revision `5667c4acecd0981ae5114cbbf2ee3dcccec9c248`; the fixture verifies image revision labels. Set the image variables to their locally inspected image IDs. Do not run an AMD64 image under ARM emulation for qualification.

The fixture uses the actual gateway renderer, public route allowlist and dashboard identity-header stripping. Only local port, certificate paths and backend addresses differ. It exposes gateway TLS on loopback `28443`; the partner API stays pod-private. The test first rejects unprovisioned issuer users, provisions two immutable identities through the partner API, then verifies their sessions and administrator denial. Bob remains unprovisioned as a negative case. The Keycloak user named `admin` is an observability administrator, **not** a PriceTag administrator.

This test requires a fresh metering database and runs its provisioning sequence once. Rerunning the successful-user browser check does not prove the initial rejection phase. The separate `oidc-client.cjs` issuer-only test also uses port 28443 and must not run alongside this gateway fixture.

Qualified locally: issuer login through the rendered Praxis boundary, exact authenticated login identity, unknown-identity denial, no implicit administrator elevation, anonymous partner-write denial and public partner-API rejection. Existing PriceTag sessions and subsequent SSO login are denied after API deactivation; the same issuer session reaches observability without another password prompt, subject to its independent group gate. Separate local checks below cover current-role changes, shared logout and MaaS inference/accounting. Shortened benchmark phases also pass; full-duration RHEL benchmarking remains pending.

```sh
python3 tests/external-services/local.py down
```

## Real gateway telemetry and dashboard

After starting the real gateway fixture above, run this once on its fresh instance:

```sh
python3 tests/external-services/gateway-signals.py --praxis-image "$PRAXIS_IMAGE"
NODE_EXTRA_CA_CERTS="$PWD/.state/$ENMAAS_LAB_INSTANCE/ca.pem" \
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/gateway-dashboard.cjs "$PWD/.state/$ENMAAS_LAB_INSTANCE"
```

The signal test restarts only the fixture gateway with OTLP export enabled. It sends ten HTTP requests, verifies distinct routed-request series in Prometheus, and finds fresh traces in Tempo without injected authorization/cookie markers. The dashboard check authenticates to Perses, renders four panels and verifies finite metric samples. This is actual gateway traffic, but **not yet inference/accounting or host-capacity qualification**.

The native `enmaas-gateway` dashboard reports routed request rate, mean routed duration, active public requests and open file descriptors. Routed counters exclude pre-routing rejections; mean duration is not a percentile. Keeping cluster/listener/route labels prevents distinct metric series from collapsing during redaction. Do not infer full benchmark capacity from these checks.

## Authenticated mock inference

For a fresh gateway fixture, add `--maas-image "$MAAS_IMAGE"` to the `gateway-sso.py start` command. The MaaS image must use native architecture and revision `6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d`; the fixture checks its revision label. This adds private MaaS and deterministic mock backends without publishing more ports. It uses the fixture database, not an external database.

```sh
python3 tests/external-services/gateway-sso.py inference
```

Run once per fresh fixture. The probe creates a dedicated user, a USD budget, explicit synthetic rates and a one-hour key through management APIs. It sends JSON and streaming requests through the public TLS gateway, revokes the key and disables the user. A separate read-only database query requires exactly two usage events and the expected token totals. Keys remain inside the disposable probe process; evidence contains only the synthetic subject and counts.

This checks inference and token accounting. It also checks the exact recorded USD charge at the synthetic rates. It does not qualify discounts, concurrent quota enforcement, the complete workload sequence or capacity. All service peers share the trusted local fixture namespace; RHEL peer isolation remains separate acceptance work.

## Replaceable provider profiles

For a **fresh** MaaS-enabled fixture, add `--mock-profiles` to `gateway-sso.py start`. This publishes the same `openai/` and `external/` model aliases as the real installer, with deterministic private mock backends. It reads model limits from reviewed presets and keeps imported prices instead of replacing them with synthetic rates. See [provider management](providers.md) for profile ownership and RHEL conversion to real endpoints.

After the identity tests, exercise every configured alias through its native API and verify imported-price accounting:

```sh
python3 tests/external-services/profile-inference.py --praxis-image "$PRAXIS_IMAGE"
```

This creates and retires one test identity; run it separately from a benchmark. It does not change imported model rates.

The default benchmark uses the first OpenAI-compatible mock alias. Select another with `local-benchmark.py --model external/gpt-5.4-mini` (along with its usual image/duration options). The exact-price check requires an imported price row and reconciles stored per-request rounding. The legacy `gateway-sso.py inference` probe is for the original `fixture` configuration; do not use it on this profile variant.

## Local workload sequence

With the MaaS-enabled fixture still running, use the same six phases as the RHEL runner:

```sh
python3 tests/external-services/local-benchmark.py \
  --praxis-image "$PRAXIS_IMAGE" --duration 600 --offered-rps 30
```

The duration is per phase. Use a short run for troubleshooting; use at least 65 seconds to include an active/idle burst cycle. The default is ten minutes per phase. The local generator opens a new TLS connection per request and runs on the workstation; results are not a prediction of RHEL capacity.

The runner creates isolated users and finite keys, changes only the fixture gateway rate limiter, captures container resource samples and checks per-user events, tokens and stored USD charges. It restores the gateway configuration, disables the benchmark users and revokes their keys on exit. Private `summary.json`, `resources.jsonl` and `cleanup.json` remain under the named fixture state. Inspect `cleanup.json` after any failure. Do not run other fixture-management commands concurrently.

Preflight requires at least 10% available Linux host memory and 30% available disk space; these checks continue during traffic. A capacity stop reports the threshold without printing credentials. On macOS, the checked host is the Podman Linux VM. Keep data volumes and previous results when freeing space; do not run a broad Podman prune.

## Inspect the results

Use the [offline report instructions](report.md) to graph recorded CPU and memory alongside the workload outcomes.

If the telemetry relay was enabled with `gateway-signals.py` before the benchmark, the runner preserves its OTLP settings across restarts and requires fresh inference metrics and traces after every phase. Use phase durations of at least 35 seconds to allow multiple 15-second scrapes. Telemetry is evidence of observation, not a replacement for independent usage-ledger reconciliation.

## Current-role and logout regression

`gateway-lifecycle.cjs` exercises an existing provisioned synthetic administrator session: promotion, demotion, non-mutating GET logout, confirmed issuer logout, reauthentication and the separate observability-cookie refresh window. It requires the pinned source-build revision described in [OIDC qualification](oidc.md); the older published image is not qualified for this flow.

The test uses only the fixed synthetic `admin@example.test` identity, restores its ordinary user role and records credential-free evidence. A passing local patched-image test does not establish instantaneous global logout or RHEL qualification. The observability role remains a separate, explicit Keycloak group assignment.

## Host-resource dashboard

With the named gateway fixture above running, send aggregate measurements from its Linux host:

```sh
(
LAB_STATE="$PWD/.state/${ENMAAS_LAB_INSTANCE:?Start the named gateway fixture first}"
python3 tests/external-services/local-host-metrics.py
NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
  NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/gateway-dashboard.cjs "$LAB_STATE" host
)
```

Use the same `ENMAAS_LAB_INSTANCE` and `LAB_STATE` as the running fixture. The helper reads only aggregate CPU, memory, load and filesystem capacity; on macOS it runs the sampler inside the Podman machine. Perses exposes the **enMAAS gateway — host resources** dashboard through its existing SSO gate. CPU needs at least two samples. This qualifies the sample/transport/dashboard path, not the RHEL systemd timer or host firewall policy.

For the full six-phase benchmark, start a longer sampler in another terminal with the same repository and `ENMAAS_LAB_INSTANCE`:

```sh
python3 tests/external-services/local-host-metrics.py --samples 150 --interval 30
```

This supplies roughly 75 minutes of host metrics, including setup time. It is read-only on the Linux host and can run alongside the benchmark. Container resource samples are also captured independently by the benchmark runner.

## Trace navigation

With the named gateway fixture and its telemetry relay running:

```sh
NODE_EXTRA_CA_CERTS="$PWD/.state/$ENMAAS_LAB_INSTANCE/ca.pem" \
  NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/trace-dashboard.cjs "$PWD/.state/$ENMAAS_LAB_INSTANCE"
```

This creates gateway login-page requests, authenticates to Perses, queries stored gateway traces and selects a table link to inspect spans. It saves a private screenshot. Raw Tempo ports remain unpublished. The trace view intentionally lacks names and attributes removed by the telemetry privacy policy.


## Container logs and Loki view

With the named gateway fixture running:

```sh
python3 tests/external-services/log-runtime.py
NODE_EXTRA_CA_CERTS="$PWD/.state/$ENMAAS_LAB_INSTANCE/ca.pem" \
  NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/logs-dashboard.cjs "$PWD/.state/$ENMAAS_LAB_INSTANCE"
```

The runtime test creates an isolated writer and collector with private volumes/secrets, emits synthetic sensitive-text sentinels through real Podman stdout, and verifies that only sanitized events reach Loki. It tests receiver restart and in-place file truncation, then removes its own containers, secrets and volumes. The browser test reads those events and checks the observability group boundary. This does not qualify RHEL SELinux or promise lossless logs during arbitrary rotation/outages.

## Existing-session group revocation

Use a **fresh disposable fixture**, with other local fixtures stopped because they share loopback ports. This opt-in adds a confidential test client with user-management/query roles to this local realm only; the RHEL generator does not enable it. Its secret stays in the private fixture directory. Keycloak's `kc-admin-api` scope supplies the audience required by its management API.

```sh
export ENMAAS_LAB_INSTANCE=external-revocation-scoped
python3 tests/external-services/local.py start --authorization-test
NODE_EXTRA_CA_CERTS="$PWD/.state/$ENMAAS_LAB_INSTANCE/ca.pem" \
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
node tests/external-services/group-revocation.cjs ".state/$ENMAAS_LAB_INSTANCE"
python3 tests/external-services/local.py down
```

The test signs in to Perses, removes the synthetic administrator's observability group through the issuer API, and requires the existing cookie to lose access within 90 seconds. It restores group membership in cleanup. A backend error is not accepted as proof of denial. The configured refresh interval is one minute; this is bounded refresh-based revocation, not instantaneous invalidation. If cleanup fails, inspect the disposable realm before reusing it.
