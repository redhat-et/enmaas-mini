# Mock and real providers

## Profiles and ownership

The real installer supports two profiles: `openai` (OpenAI directly) and `external` (OpenAI-compatible and/or Anthropic Messages APIs). The default `--mode mock` configuration uses **the same profile names, model aliases and credential slots** with private deterministic backends. It is intended for testing replacement without changing users, budgets or keys.

| Profile | Example alias | Mock destination |
| --- | --- | --- |
| `openai` | `openai/gpt-5.4-mini`, `openai/gpt-6-luna` | Private OpenAI-compatible fixture |
| `external` | `external/gpt-5.4-mini`, `external/rits/zai-org/glm-5-3` | Private OpenAI-compatible fixture |
| `external` | `external/claude-sonnet-5-5`, `external/rits/zai-org/glm-5-3` | Private Anthropic Messages fixture |

Model limits come from the repository's reviewed presets. The mock advertises and serves only its configured models; it does not validate real-provider availability, model quality or real context capacity. Request and token counts are synthetic. PriceTag imports pricing independently; aliases do not manufacture provider prices. Inspect the actual rates before drawing cost conclusions.

## Install a replaceable mock deployment

Use the [headless mock installation](headless.md). `--mode mock` selects these profiles by default; `--mock-profiles` can state that choice explicitly. It stores the same provider inputs used by real setup under `/root/pricetag-provider-inputs`, supplies synthetic credentials and starts the private mock service. Existing legacy deployments with only a `fixture` or `mock/fixture` model do not have these provider records; use a fresh installation for this conversion flow. `--demo-fixture` retains the separate legacy demo configuration.

The local Podman gateway fixture also accepts `gateway-sso.py start --mock-profiles` with its three reviewed images. That fixture runs in a shared test pod and is **not** a supported host-management deployment: do not run the root/systemd provider helper against your workstation or add real credentials to its shared database/network fixture. Use the RHEL host-managed benchmark installation for this conversion flow; it does not apply to the disposable single-pod test fixture. The host replacement commands below are for the installed RHEL gateway.

## Inspect providers and models on RHEL

From the repository directory on the gateway:

```sh
sudo python3 scripts/pricetag/providers list
```

Discover a real external provider's models without changing the deployment:

```sh
sudo python3 scripts/pricetag/providers external --api both --list-models
```

Enter reviewed HTTPS endpoint(s) and the key at the hidden prompt. Discovery lists IDs; it does not supply authoritative prices or limits. See [provider management](../../quickstarts/remote-gateway-pricetag/providers.md) for model selection, protocol headers, pricing and client refresh.

## Convert one profile to a real provider

**Stop benchmarking first.** Real requests may incur charges. Run these commands on the gateway; select only the profile you intend to replace.

Direct OpenAI, preserving the existing model selection:

```sh
sudo python3 scripts/pricetag/providers openai --replace
```

External provider, replacing endpoints/key and refreshing its model selection:

```sh
sudo python3 scripts/pricetag/providers external --api both --replace --refresh-models
```

Use `--api openai` or `--api anthropic` for a single protocol. Review `configs/pricetag/provider-models.json` first. The helper performs discovery, writes the selected profile's catalog/routing/credential references, restarts Praxis and checks readiness. Failed activation rolls back. Users, keys, budgets and usage history remain; historical mock data is not turned into real usage or erased.

For unattended replacement, add `--key-file /root/provider-keys/NAME.key` and the explicit external `--openai-url`/`--anthropic-url` arguments. Key files must meet the helper's root-owned private-file checks. Never pass credential values on the command line.

```sh
sudo python3 scripts/pricetag/providers list
```

Confirm the new catalog and make an intentional small inference request. Refresh the local harness configuration after model changes. The load runner must refuse once either profile uses real credentials or endpoints, including a mixed mock/real deployment. Do not weaken that guard to benchmark a paid upstream.

The unselected profile remains mocked until replaced. The private mock service may remain running but is no longer routed by a fully converted deployment; stopping/removing that unused service is a separate reviewed cleanup step. For real demonstrations, prefer a fresh deployment so mock history cannot be mistaken for actual spend.

## Qualification

Configuration tests cover standard profile metadata, route replacement and rejection of real credentials/endpoints by the mock guard. Native protocol tests cover advertised models, unknown-model denial and required credentials. Full fresh-host replacement and RHEL activation/rollback still require the supplied VM; do not treat offline tests as that acceptance evidence.
