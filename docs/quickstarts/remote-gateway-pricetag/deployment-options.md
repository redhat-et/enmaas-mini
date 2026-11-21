# Choose a deployment

Install the gateway on RHEL. Provision its infrastructure separately; AWS
credentials are never inputs to the application installer.

| Layout | Gateway services | Optional external services |
| --- | --- | --- |
| **One node** | Praxis, MaaS, PriceTag Metering and local PostgreSQL | External PostgreSQL |
| **Gateway with remote services** | The same gateway and provider configuration | OIDC browser login, PostgreSQL, observability |

Both layouts can use real providers. Mock providers and load generation are
separate testing options. External-service integration passes local container
checks; separate-host RHEL, restart recovery and public access still require
qualification. Inference uses MaaS keys; OIDC authenticates browser users.

## Install the gateway

- **Interactive:** follow the [quickstart](README.md), select providers and enter
  credentials at hidden prompts.
- **Headless:** follow [non-interactive installation](headless.md), using protected
  credential files and explicit choices. `--yes` alone does not disable prompts.
- **Remote services:** follow [external-service integration](full-deployment.md).
  The gateway wizard does not create the issuer or observability stack.

These are fresh-install paths. Existing deployment state is refused; rerunning
the installer is not an upgrade procedure. [Image selection](images.md) explains
published images versus explicitly pinned source builds.

## Prepare infrastructure for testing

Use existing suitable hosts, or the separate testing helpers:

- [One RHEL VM](../../testing/aws-pricetag.md).
- [Three-host AWS infrastructure](../../testing/external-services/aws.md).
- [Optional external PostgreSQL](external-database.md) and
  [private RDS planning](../../testing/external-services/rds.md).
- [Disposable local Podman stack](../../testing/external-services/local-podman.md).

The AWS helpers create billable resources. Read their ownership and cleanup
limits before provisioning. A successful local Podman test does not validate
AWS permissions or RHEL host security.
