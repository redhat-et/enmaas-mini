# Headless lab installation

Provision the hosts from the [separate AWS lab guide](aws.md), then apply [peer rules](network.md), distribute [certificates](certificates.md), and prepare the [issuer](keycloak-rhel.md) and [observability](observability-rhel.md). Their installers take explicit arguments and have no menus. The observability generator requires both its public address and `--private-ip`; use the private IP printed by `aws_lab_info`. Its server certificate must cover both addresses. AWS launch/RDS confirmations remain separate administrator operations.

Run application installation **on the gateway**, from the reviewed checkout. `--non-interactive` disables prompts even when a terminal is attached. Missing choices or credential-file paths fail before installation. `--yes` explicitly authorizes the installation; it does not supply missing credentials.

## Mock benchmark gateway

Set the gateway address and use the root-private OIDC file transferred by the issuer instructions:

```sh
export GATEWAY_HOST_IP='REPLACE_WITH_GATEWAY_PUBLIC_IPV4'
sudo python3 scripts/pricetag/install.py \
  --non-interactive --yes --mode mock --mock-profiles \
  --hostname "$GATEWAY_HOST_IP" --no-sample-users --monthly-usd 5 \
  --image-mode source \
  --maas-revision 6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d \
  --metering-revision 2281e1a84bfc3b998e7db36bc01bae88130bfcdd \
  --tls-bundle /root/enmaas-tls \
  --oidc-config /root/enmaas-inputs/oidc.json
```

This selects replaceable `openai`/`external` mock profiles; see [provider management](providers.md). An SSH-created administrator remains available for recovery. OIDC identities are provisioned separately using the issuer UUIDs and the [identity helper](oidc.md#provision-an-identity-without-sql). Do not create duplicate Alice/Bob identities with unrelated UUIDs. The benchmark provisions and retires its own test users.

## Real providers

Use the same explicit image/TLS/OIDC options, replacing `--mode mock` with `--mode real` and the desired provider arguments. Store keys in root-owned mode-`0600` files; never put their values in arguments, exported variables or shell history.

| Provider | Arguments to add |
| --- | --- |
| OpenAI directly | `--provider openai --openai-key-file /root/enmaas-inputs/openai.key` |
| External OpenAI-compatible API | `--provider external --external-api openai --external-openai-url https://provider.example/v1 --external-key-file /root/enmaas-inputs/external.key` |
| External Anthropic Messages API | `--provider external --external-api anthropic --external-messages-url https://provider.example --external-key-file /root/enmaas-inputs/external.key` |
| Both external protocols | `--provider external --external-api both --external-openai-url https://openai.example/v1 --external-messages-url https://messages.example --external-key-file /root/enmaas-inputs/external.key` |

Replace example URLs with the reviewed provider endpoints. For direct OpenAI plus an external provider, supply both sets of arguments. One external key must authorize both endpoints when selecting `both`; Messages defaults to `x-api-key`, with `--external-messages-auth bearer` available where required. Provider discovery remains authenticated and may fail if the supplied key or endpoint is invalid.

## Optional external databases

After [RDS role bootstrap](rds.md#bootstrap-application-credentials), add these options to the same installation command:

```sh
--maas-database-url-file /root/pricetag-external-db/maas.url \
--metering-database-url-file /root/pricetag-external-db/metering.url \
--external-database-ca-file /root/pricetag-external-db/ca.crt
```

These are arguments, not a separate command. Both protected URL files are required; use the verified RDS CA bundle. Omit all three options to use the gateway's private local database.

## Telemetry, verification and cleanup

Connect the gateway with the explicit [telemetry generator and installer](gateway-telemetry.md). Its receiver uses the observability **private IP**. Then run the [benchmark command](benchmark.md) against mocked backends; use the six default ten-minute phases for the RHEL acceptance run.

Service installers do not create AWS infrastructure and do not need AWS credentials. AWS deletion is an administrator operation using the [cleanup inventory](cleanup.md). No installation command automatically deletes existing application state or recovers a partial installation by overwriting credentials.

The headless parser and local source build are tested. Fresh RHEL installation, firewall/SELinux/reboot, inter-host networking and actual RDS acceptance remain pending supplied infrastructure.

## Interactive gateway installation

The issuer and observability steps above still run with explicit arguments. Once
their inputs and the gateway certificate bundle are in place, run on the gateway
with an attached terminal:

```sh
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST_IP" \
  --mode mock --mock-profiles --image-mode source --no-sample-users \
  --maas-revision 6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d \
  --metering-revision 2281e1a84bfc3b998e7db36bc01bae88130bfcdd \
  --tls-bundle /root/enmaas-tls --oidc-config /root/enmaas-inputs/oidc.json
```

This asks for final confirmation. Mock mode needs no real provider keys. For real
providers, use `--mode real` instead of the two mock flags and let the gateway
installer prompt for providers and credentials. Do not run the mock-only benchmark
after configuring real providers. This is not an upgrade command.
