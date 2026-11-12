# Choose a deployment and installation mode

Service installation runs on RHEL. Provision infrastructure separately, using your
own platform or the linked AWS testing helpers. AWS credentials stay on the
workstation; they are not service-installer inputs.

## Deployment choices

| Choice | Services | Database | Login and monitoring |
| --- | --- | --- | --- |
| **One node** | Praxis, MaaS and PriceTag on one RHEL host | Local PostgreSQL by default; optional external MaaS and metering databases | User keys; no SSO or external observability configured |
| **Full deployment** | Gateway node plus separate Keycloak and observability nodes | Local PostgreSQL by default; optional external gateway databases | OIDC browser login; Perses with Prometheus, Loki, Tempo and collectors |

These are two production deployment layouts, not a production/test distinction.
Both can use real providers. Mock providers and benchmarks are separate testing
options. The full integration is still under RHEL qualification: its production
intent does not establish readiness for unrestricted public exposure. Inference
uses user keys; SSO is for browser login.

## One node: interactive

Follow the [main quickstart](README.md) to copy the checkout and run the installer
through SSH. On the RHEL host itself, from the installed checkout:

```sh
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST"
```

Select providers, enter keys at hidden prompts, choose initial users and confirm.
Do not pass `--oidc-config` or install the telemetry agent for this variant.
For external PostgreSQL, prepare its protected files and add the options from
[external database setup](external-database.md); the provider prompts still work.

## One node: headless

From the checkout on RHEL, with a direct OpenAI key already in the root-owned,
mode-0600 file shown below:

```sh
sudo scripts/pricetag/install --non-interactive --yes \
  --hostname "$GATEWAY_HOST" --mode real --image-mode published \
  --provider openai --openai-key-file /root/enmaas-inputs/openai.key \
  --no-sample-users --monthly-usd 5
```

[Provider setup](providers.md) covers external OpenAI-compatible and Anthropic
Messages endpoints. Supply the appropriate protected key files and explicit URLs;
never put secret values into arguments or shell history. Headless mode fails when
required choices are missing; `--yes` alone does not disable prompts.

For a mock-only one-node test, no provider credentials are needed:

```sh
sudo scripts/pricetag/install --non-interactive --yes \
  --hostname "$GATEWAY_HOST" --mode mock --mock-profiles \
  --image-mode published --no-sample-users --monthly-usd 5
```

Published images and source candidates may expose different UI features; see
[image selection](images.md). Existing installations are refused: these commands
are fresh-install paths, not upgrades.

## Full deployment: installation order

1. Prepare three fresh RHEL hosts and restricted peer networking.
2. Generate role-specific certificates on the workstation; distribute only leaf keys.
3. Install Keycloak; transfer each service's own client configuration.
4. Install observability and the gateway; provision immutable SSO identities.
5. Install the gateway telemetry agent; verify login and access boundaries.
6. Verify authenticated inference, PriceTag usage and observability data.

Follow [full deployment](full-deployment.md) for service integration, configuration
inputs and interactive/headless gateway installation. Existing external services
can be used; dedicated test installers for them live under `tests/`. There is no
single interactive wizard that provisions all three hosts.

## Optional AWS test infrastructure

- [One RHEL node](../../testing/aws-pricetag.md).
- [Three-node test lab](../../testing/external-services/aws.md).
- [Optional private RDS](../../testing/external-services/rds.md).

These helpers are for testing and create billable resources. Review their current
limitations and cleanup instructions before provisioning. Neither deployment mode
requires AWS when suitable RHEL hosts and networking already exist.
