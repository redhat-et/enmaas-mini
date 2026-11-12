# Full deployment: external SSO and observability

Use this layout for Praxis, MaaS and PriceTag on RHEL with an external OIDC issuer
and external observability. PostgreSQL can remain local or use external MaaS and
metering databases. Real providers are configured in the gateway installer.

For the layout without SSO/observability, follow the [one-node quickstart](README.md).
For synthetic workloads, use [full-deployment testing](../../testing/external-services/README.md).
AWS is optional; its provisioning helpers are separate from installation.

**Qualification:** source-pinned OIDC and telemetry flows pass local tests.
Separate-host RHEL, reboot recovery and unrestricted public exposure are not yet
qualified. Use restricted access for the first deployment and review the
[security acceptance criteria](../../testing/external-services/security-acceptance.md).

## 1. Prepare the services and connectivity

| Component | Required input | Setup reference |
| --- | --- | --- |
| Gateway RHEL host | SSH access, public HTTPS identity, reviewed checkout | [Copy the checkout](README.md#2-copy-the-checkout) |
| OIDC issuer | Exact issuer URL, confidential client, secret, callback and immutable identity claim | [OIDC contract](../../testing/external-services/oidc.md) |
| Observability | Private OTLP receiver with client-certificate authentication; restricted dashboard access | [Collector placement](../../testing/external-services/topology.md) |
| HTTPS and peer trust | Host certificates and trust bundles; collector client key/certificate | [Certificate setup](../../testing/external-services/certificates.md) |
| PostgreSQL | Local by default; two protected connection files for external databases | [External database setup](external-database.md) |

Use existing external services when they meet these contracts. For evaluation on
fresh RHEL hosts, the [Keycloak installer](../../testing/external-services/keycloak-rhel.md)
and [observability installer](../../testing/external-services/observability-rhel.md)
prepare dedicated services with explicit command-line inputs. They are test tooling,
not a qualified production identity/observability lifecycle manager.

Keep database/query ports private. Use the [private issuer path](../../testing/external-services/oidc.md#private-issuer-resolution)
and [peer rules](../../testing/external-services/network.md#dns-issuer-with-private-peer-traffic)
for inter-host authentication traffic. Only client-facing HTTPS and source-restricted
SSH should be externally reachable. A shared issuer does not synchronize PriceTag
roles with the observability access group.

## 2. Prepare protected gateway inputs

On the gateway, prepare these files using the linked setup instructions:

- `/root/enmaas-inputs/oidc.json`: issuer/client settings and secret; root-owned, mode `0600`.
- `/root/enmaas-tls/`: gateway HTTPS leaf key/certificate, public trust and dedicated
  telemetry client credentials. Keep CA signing keys off the hosts.
- Provider key files for headless installation; interactive installation prompts
  for them instead. Never put key values in command arguments.

For external databases, also prepare the protected connection and CA files from
[external PostgreSQL](external-database.md). Database credentials and issuer/client
secrets serve different purposes; do not reuse them.

## 3. Install the gateway

Run from the installed checkout **on RHEL**. Set `GATEWAY_HOST` to its HTTPS IP or
DNS identity. Source mode is currently required for the locally qualified OIDC
lifecycle; the lab's published-image mode is not yet qualified for that flow.

### Interactive

```sh
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --mode real --image-mode source --no-sample-users \
  --maas-revision 6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d \
  --metering-revision 2281e1a84bfc3b998e7db36bc01bae88130bfcdd \
  --tls-bundle /root/enmaas-tls --oidc-config /root/enmaas-inputs/oidc.json
```

Select providers and enter credentials at hidden prompts. Confirm installation.
The external-service configurations above are prepared beforehand; the interactive
gateway installer does not create the issuer, observability services or AWS resources.

### Headless

With a direct OpenAI key already stored in `/root/enmaas-inputs/openai.key`:

```sh
sudo scripts/pricetag/install --non-interactive --yes \
  --hostname "$GATEWAY_HOST" --mode real --image-mode source \
  --no-sample-users --monthly-usd 5 \
  --maas-revision 6ac4c80ebc95f590e5f24593ac15bb80c7f9fa1d \
  --metering-revision 2281e1a84bfc3b998e7db36bc01bae88130bfcdd \
  --tls-bundle /root/enmaas-tls --oidc-config /root/enmaas-inputs/oidc.json \
  --provider openai --openai-key-file /root/enmaas-inputs/openai.key
```

For external OpenAI-compatible or Anthropic Messages services, substitute the
[explicit provider arguments](../../testing/external-services/headless.md#real-providers).
Both installation modes accept the [external database flags](external-database.md).
These commands create a fresh installation; they do not upgrade existing state.

## 4. Connect telemetry and provision users

1. Install the [gateway collector agent](../../testing/external-services/gateway-telemetry.md)
   with the observability receiver's private address and the gateway's own client certificate.
2. [Provision the issuer's immutable user IDs](../../testing/external-services/oidc.md#provision-an-identity-without-sql)
   through the partner API. Assign roles/budgets separately with the management API;
   matching email or issuer group names do not grant administrator access.
3. Keep the SSH-created administrator for recovery until SSO administration is tested.
4. Issue inference keys and configure [OpenCode](clients.md). Browser SSO does not
   replace the inference key configured in a harness.

## 5. Verify and operate

- PriceTag: `https://GATEWAY_HOST:8443/login`. Verify SSO, current-role access and
  per-user/team usage after authenticated inference.
- Perses: the external observability HTTPS URL. The supplied RHEL test installer
  uses `https://OBSERVABILITY_HOST:8443/` and a separate administrator-group gate.
- Check metrics, host resources, traces and service events. Raw Prometheus, Loki,
  Tempo and database ports must remain private.
- Follow [providers](providers.md), [users and budgets](users.md), and
  [management APIs](management-api.md) for day-to-day changes.

For an isolated mock run, follow [benchmark setup](../../testing/external-services/headless.md#mock-benchmark-gateway)
then [benchmark execution](../../testing/external-services/benchmark.md).
The runner refuses real-provider deployments; do not benchmark them by bypassing that check.

## Optional test infrastructure

[AWS three-node provisioning](../../testing/external-services/aws.md) ·
[Optional RDS](../../testing/external-services/rds.md) ·
[Local Podman qualification](../../testing/external-services/local-podman.md) ·
[Cleanup](../../testing/external-services/cleanup.md)
