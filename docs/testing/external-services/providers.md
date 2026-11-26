# Mock and real providers

This is the **testing-to-real-provider** flow for an installed RHEL gateway.
For a new deployment with real providers from the start, use the
[one-node quickstart](../../quickstarts/remote-gateway-pricetag/README.md) or
[gateway with remote services](../../quickstarts/remote-gateway-pricetag/full-deployment.md).
The provider-management commands are the same in both layouts; remote SSO,
observability and database settings do not change when a provider is replaced.

## Choose the operation

| Starting point | Operation |
| --- | --- |
| Fresh production host | Use the production quickstart and configure real providers during installation. |
| Installed mock gateway | Stop the benchmark, then replace one or both provider profiles below. |
| Installed real gateway | Use the same replacement commands to change credentials, endpoints or models. |

Replacing a provider does not change the remote OIDC issuer, telemetry receiver
or database configuration. Those services remain connected.

## Profiles and ownership

The installer supports two profiles: `openai` (OpenAI directly) and `external` (OpenAI-compatible and/or Anthropic Messages APIs). When explicitly selected, `--mode mock` uses **the same profile names, model aliases and credential slots** with private deterministic backends. Real-provider mode remains the installer default.

| Profile | Example alias | Mock destination |
| --- | --- | --- |
| `openai` | `openai/gpt-5.4-mini`, `openai/gpt-6-luna` | Private OpenAI-compatible fixture |
| `external` | `external/gpt-5.4-mini`, `external/rits/zai-org/glm-5-3` | Private OpenAI-compatible fixture |
| `external` | `external/claude-sonnet-5-5`, `external/rits/zai-org/glm-5-3` | Private Anthropic Messages fixture |

Model limits come from the repository's reviewed presets. The mock advertises and serves only its configured models; it does not validate real-provider availability, model quality or real context capacity. Request and token counts are synthetic. PriceTag imports pricing independently; aliases do not manufacture provider prices. Inspect the actual rates before drawing cost conclusions.

## Install a replaceable mock deployment

Use the [headless mock installation](../../quickstarts/remote-gateway-pricetag/headless.md#mock-installation). `--mode mock` selects these profiles by default; `--mock-profiles` can state that choice explicitly. It stores the same provider inputs used by real setup under `/root/pricetag-provider-inputs`, supplies synthetic credentials and starts the private mock service. Existing legacy deployments with only a `fixture` or `mock/fixture` model do not have these provider records; use a fresh installation for this conversion flow. `--demo-fixture` retains the separate legacy demo configuration.

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

**Stop benchmarking first and save its report.** For a detached run, use its
printed `systemctl stop` command, then verify `cleanup.json` reports successful
cleanup before proceeding. If cleanup failed, resolve that failure before replacing
providers. See [benchmark completion and cleanup](benchmark.md#run-independently-of-the-ssh-connection).
Real requests may incur charges.
For a coordinated installation, reprint commands from the saved configuration on
the workstation:

```sh
python3 tests/external-services/services.py --config "$SERVICE_CONFIG" --access
```

Under `provider_replacement`, copy the command for the chosen profile. It connects
to the recorded gateway and checkout with verified SSH and opens hidden credential
prompts. For a single external protocol, change `--api both` to `--api openai` or
`--api anthropic`. No replacement runs merely by printing this summary.

For manual installation, SSH to the gateway and run from the same installed
repository directory used for installation. Select only the profile you intend to replace; do not rerun the
fresh-install command. From the workstation, use the gateway SSH settings saved
for this deployment (not the Keycloak or observability host):

```sh
ssh -i "$SSH_KEY" "$RHEL_HOST"
```

On the gateway, change into the installed checkout. For a coordinated installation,
its path is `/opt/enmaas-mini/<deployment-id>` from the service configuration; for
a manual installation, use the directory where you ran the installer. Run the
following commands there.

Direct OpenAI, preserving the existing model selection:

```sh
sudo python3 scripts/pricetag/providers openai --replace
```

External provider, replacing endpoints/key and refreshing its model selection:

```sh
sudo python3 scripts/pricetag/providers external --api both --replace --refresh-models
```

Use `--api openai` or `--api anthropic` for a single protocol. Review `configs/pricetag/provider-models.json` first. The helper performs discovery, writes the selected profile's catalog/routing/credential references, restarts Praxis and checks readiness. Failed activation attempts to restore the previous gateway files and restart it; a recovery failure reports the private backup path. New price records and old credential versions can remain for review. Users, keys, budgets and usage history remain; historical mock data is not turned into real usage or erased.

### Headless replacement

Prepare root-owned private credential files through your secret-delivery process.
Use mode `0600`; never put credential values on the command line. Run either or
both commands, depending on which profiles you want to replace:

```sh
sudo python3 scripts/pricetag/providers openai --replace \
  --key-file /root/enmaas-inputs/openai.key
```

```sh
sudo python3 scripts/pricetag/providers external --api both \
  --replace --refresh-models \
  --openai-url https://openai.example/v1 \
  --anthropic-url https://messages.example \
  --key-file /root/enmaas-inputs/external.key
```

Replace the example URLs. For one external protocol, select `--api openai` or
`--api anthropic` and supply only its URL. For both protocols, the key must be
authorized at both endpoints. Messages uses `x-api-key` by default; select
`--messages-auth bearer` only when required by your provider.

### Verify the replacement

```sh
sudo python3 scripts/pricetag/providers list
```

Check the effective rates in PriceTag before inference; retained prices may be
test overrides. Use [pricing management](../../quickstarts/remote-gateway-pricetag/providers.md)
to review model limits and prices. Refresh the [client configuration](../../quickstarts/remote-gateway-pricetag/clients.md)
after model changes, make one intentional small authenticated request, and verify
its model and usage in PriceTag. Gateway readiness alone does not prove that the
upstream accepts inference with the supplied key.

The load runner must refuse once either profile uses real credentials or endpoints,
including a mixed mock/real deployment. Do not weaken that guard to benchmark a paid upstream.

The unselected profile remains mocked until replaced. The private mock service may remain running but is no longer routed by a fully converted deployment; stopping/removing that unused service is a separate reviewed cleanup step. For real demonstrations, prefer a fresh deployment so mock history cannot be mistaken for actual spend.

## Qualification

Configuration tests cover standard profile metadata, route replacement and rejection of real credentials/endpoints by the mock guard. Native protocol tests cover advertised models, unknown-model denial and required credentials. Full fresh-host replacement and RHEL activation/rollback still require the supplied VM; do not treat offline tests as that acceptance evidence.
