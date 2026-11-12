# Inspect the local dashboards

Start the [MaaS-enabled local gateway fixture](local-podman.md#authenticated-mock-inference) first. Use the same `ENMAAS_LAB_INSTANCE` for all commands. The stack serves real application pages and stores usage from **mocked LLM responses**; it does not call paid providers.

## 1. Select the running fixture

Run from the repository checkout:

```sh
export ENMAAS_LAB_INSTANCE=external-demo
export LAB_STATE="$PWD/.state/$ENMAAS_LAB_INSTANCE"
python3 tests/external-services/local.py check
```

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

This calls the fixture's private management API; it does not change Keycloak groups or any RHEL server. Demote it when finished (`gateway-sso.py demote`) and before rerunning the current-role regression test. Alice is disabled and Bob unprovisioned by the negative identity tests; they are not ready-made demo personas in this fixture.

## 4. Populate and refresh data

Enable the [gateway telemetry relay](local-podman.md#real-gateway-telemetry-and-dashboard) once before benchmarking, then run the [local workload sequence](local-podman.md#local-workload-sequence). The default is six ten-minute phases; 65 seconds per phase is a useful shorter run that includes the burst idle interval.

Run `profile-inference.py --praxis-image "$PRAXIS_IMAGE"` separately to add usage for every mock alias and both native API protocols. It verifies the imported price records and retires its test key.

Select a dashboard time range covering the run, such as the last hour. The runner retires its test users and keys afterward; their usage history remains in PriceTag. Expect benchmark-generated user names, not Alice/Bob attribution. PriceTag tracks accounting; Perses tracks operational signals. Short runs have sparse resource samples and do not establish capacity.

The local metrics/trace relay does not mount application log files. The separate [log runtime test](local-podman.md#container-logs-and-loki-view) populates the log view with sanitized test events. The RHEL telemetry installer mounts the three explicit service log files. Do not confuse synthetic log test events with captured application logs.

## 5. Stop without deleting history

```sh
python3 tests/external-services/gateway-sso.py demote
python3 tests/external-services/local.py down
```

This stops the named fixture and removes its labelled secrets; volumes and private evidence remain. Full removal, including browser trust, is documented in [cleanup](cleanup.md). Never run cleanup or fixture reconfiguration during a benchmark.
