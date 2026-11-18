# Inspect the local dashboards

Start the [MaaS-enabled local gateway fixture](local-podman.md#authenticated-mock-inference) first. Use the same `ENMAAS_LAB_INSTANCE` for all commands. The stack serves real application pages and stores usage from **mocked LLM responses**; it does not call paid providers.

## 1. Select the running fixture

Run from the repository checkout:

```sh
export ENMAAS_LAB_INSTANCE=external-demo
export LAB_STATE="$PWD/.state/$ENMAAS_LAB_INSTANCE"
python3 tests/external-services/local.py check
```

The check inspects the selected pod’s ownership and actual host-port mappings.
Only the documented loopback bindings are accepted; wildcard, extra or remapped
ports fail before readiness is reported. This does not qualify a RHEL firewall.

Replace `external-demo` if you created a different named fixture. Only one fixture can use these fixed ports. The default unnamed fixture instead stores its files in `.state/external-services-local`.

## 2. Trust the lab certificate and obtain the password

Import **only** `$LAB_STATE/ca.pem` into your test browser's certificate authorities. On macOS, Keychain Access can import it into your login keychain; explicitly trust the lab CA for SSL, then restart the browser. Remove that trust when removing the lab. Never import or share `ca.key`, `server.key`, `client.key`, or the whole state directory. The disposable certificates expire after seven days.

The local username is `admin`. On macOS, copy its generated password without printing it:

```sh
python3 -c 'import json,os,subprocess; from pathlib import Path; p=Path(os.environ["LAB_STATE"])/"credentials.json"; subprocess.run(["pbcopy"],input=json.loads(p.read_text())["admin"].encode(),check=True)'
```

Paste only into the local Keycloak login. Clear the clipboard after signing in (`printf '' | pbcopy`). Other platforms can read the same protected file locally using their password/clipboard tools; do not paste it into reports or chat.

## 3. Open PriceTag Metering and Perses

| View | URL | Login and content |
| --- | --- | --- |
| PriceTag Metering | `https://localhost:28443/login` | Choose SSO. Usage and costs come from recorded mock requests. |
| Observability | `https://localhost:19443/` | Keycloak user `admin`; explicit `observability-admin` group. Select gateway metrics, resources, traces or service events. |

The security tests intentionally leave the PriceTag test identity with ordinary user privileges. For **local visual inspection only**, grant that fixed synthetic identity the administrator role:

```sh
python3 tests/external-services/gateway-sso.py promote
```

The helper first verifies the selected pod's ownership and loopback-only port
bindings, then calls the fixture's private management API; it does not change Keycloak groups or any RHEL server. Demote it when finished (`gateway-sso.py demote`) and before rerunning the current-role regression test. Negative identity tests leave Alice disabled and Bob unprovisioned. The explicit Alice-team setup below restores Alice for a scoped manager demo; it does not grant observability access.

## 4. Populate and refresh data

Start with the [Main monitoring dashboard and sustained scenarios](monitoring.md). It links traffic, resources, accounting, alerts and inference traces.

Enable the [gateway telemetry relay](local-podman.md#real-gateway-telemetry-and-dashboard) once before benchmarking, then run the [local workload sequence](local-podman.md#local-workload-sequence). The default is six ten-minute phases; 65 seconds per phase is a useful shorter run that includes the burst idle interval.

Run `profile-inference.py --praxis-image "$PRAXIS_IMAGE"` separately to add usage for every mock alias and both native API protocols. It verifies the imported price records and retires its test key.

Select a dashboard time range covering the run, such as the last hour. The runner retires its test users and keys afterward; their usage history remains in PriceTag. Expect benchmark-generated user names, not Alice/Bob attribution. PriceTag tracks accounting; Perses tracks operational signals. Short runs have sparse resource samples and do not establish capacity.

The local metrics/trace relay does not mount application log files. The separate [log runtime test](local-podman.md#container-logs-and-loki-view) populates the log view with sanitized test events. The RHEL telemetry installer mounts the three explicit service log files. Do not confuse synthetic log test events with captured application logs.

## 5. Verify the pages and access controls

After explicitly granting the local synthetic administrator role above, run:

```sh
NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
  NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/metering-dashboard.cjs "$LAB_STATE" &&
NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/gateway-dashboard.cjs "$LAB_STATE"
```

Install the browser-test dependencies from the [local setup](local-podman.md)
first. These checks sign in as the synthetic administrator, verify populated
PriceTag APIs and charts, and check the Perses gateway panels. They do not change
roles. Evidence and screenshots stay in the private fixture directory.

`NODE_EXTRA_CA_CERTS` is required for Playwright's Node API requests. Chromium's
fixture-specific public-key pin applies to browser navigation only; it does not
configure Node's trust store. Keep certificate verification enabled. An anonymous
API request must be denied or redirected to login; a TLS error is not a passing
authorization test.

## 6. Stop without deleting history

```sh
python3 tests/external-services/gateway-sso.py demote
python3 tests/external-services/local.py down
```

This stops the named fixture and removes its labelled secrets; volumes and private evidence remain. Full removal, including browser trust, is documented in [cleanup](cleanup.md). Never run cleanup or fixture reconfiguration during a benchmark.

## Check realistic usage breakdowns

After the mixed-workload run, verify multiple models, client categories, directory
countries and cost centers as well as aggregate usage:

```sh
ENMAAS_VERIFY_REALISTIC=1 NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
  NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/metering-dashboard.cjs "$LAB_STATE"
```

The map's country outlines load from an upstream CDN. API data can be present
while the browser map is empty when that asset is blocked. This external asset
dependency is separate from telemetry delivery; inspect browser network errors.
The local transport qualification dashboard is populated by `signals.py`, not by
normal inference. Use the gateway, host, trace and service-event dashboards for
benchmark observation. Negotiated discounts remain a separate policy test. Team hierarchy is exercised by the Alice demo below.

## Browse Alice’s team

The default workload has **15 people and 10 bots**. After a successful run, attach
that run to **Alice Team Manager**, using the existing local SSO identity:

```sh
export RUN_DIR="$LAB_STATE/lab-run-REPLACE_WITH_COMPLETED_RUN"
NODE_EXTRA_CA_CERTS="$LAB_STATE/ca.pem" \
  NODE_PATH="$PWD/tests/pricetag/browser/node_modules" \
  node tests/external-services/alice-team.cjs "$LAB_STATE" "$RUN_DIR" --apply
```

This explicitly reactivates those 25 synthetic profiles for browsing, assigns
Alice as their manager through authenticated APIs, and verifies scoped usage,
charts and $300/$1,000 budgets. Their benchmark inference keys remain revoked.
Alice remains an ordinary user with a manager relationship, not a super admin;
user administration and observability access remain unavailable to her.

Use `--verify` instead of `--apply` to recheck an already attached team without changing users, roles or budgets.

Sign in as `alice` using the local fixture password and open **My team**. The
helper saves `alice-team.png` and `alice-team-evidence.json` privately. It refuses
to replace unrelated active reports: reconcile an existing team explicitly before
attaching a different run. Negative identity tests can disable Alice again.
