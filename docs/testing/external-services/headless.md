# Headless lab installation

For the developing workstation-driven command, see [coordinated service installation](service-orchestration.md).
It distributes the reviewed checkout and reuses these steps on fresh hosts; full RHEL acceptance is pending.

Provision the hosts from the [separate AWS lab guide](aws.md), then apply [reviewed peer rules without prompts](network.md#headless-application-of-a-reviewed-plan), distribute [certificates](certificates.md), and prepare the [issuer](keycloak-rhel.md) and [observability](observability-rhel.md). Their installers take explicit arguments and have no menus. The observability generator requires both its public address and `--private-ip`; use the private IP printed by `aws_lab_info`. Its server certificate must cover both addresses. AWS launch/RDS confirmations remain separate administrator operations.

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

For a fresh installation with real providers, follow the production
[full-deployment commands](../../quickstarts/remote-gateway-pricetag/full-deployment.md#headless).
They retain the remote-service configuration and supply real provider arguments
instead of mock mode.

For an already installed mock gateway, use [provider replacement](providers.md#convert-one-profile-to-a-real-provider).
Do not rerun the installer: replacement updates the selected provider in place
and preserves identities, budgets and history. Stop the mock benchmark first.

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

## After the benchmark: use real providers

Stop the traffic runner and save its report, then follow
[replace mock profiles](providers.md#convert-one-profile-to-a-real-provider).
Those commands update an installed gateway; they do not rerun installation.
They preserve users, keys, budgets, history and remote-service connections.
Review retained test prices before sending real requests.

For a fresh deployment with real providers, follow the
[production quickstart](../../quickstarts/remote-gateway-pricetag/README.md) or
[remote-services installation](../../quickstarts/remote-gateway-pricetag/full-deployment.md)
instead. Neither requires mock providers or benchmark accounts.
