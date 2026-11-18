# Full-deployment testing

This guide tests the [full deployment layout](../../quickstarts/remote-gateway-pricetag/full-deployment.md)
using mock providers. AWS infrastructure, auxiliary service fixtures and
benchmarks are testing tools; the full deployment itself is not a test-only layout.

**Qualification in progress.** Start with [local Podman tests](local-podman.md). They exercise gateway identity/session boundaries, metrics/trace delivery and six ten-minute mock traffic phases with accounting reconciliation. Three-host RHEL installation, capacity and RDS acceptance remain pending. Use mocked providers and disposable infrastructure.

## Choose a workflow

| Workflow | Start here | Outcome |
| --- | --- | --- |
| Understand the nodes | [Topology and collector placement](topology.md) | Component ownership and data flow |
| Run locally | [Local Podman setup](local-podman.md) → [dashboard access](local-dashboards.md) | Full application stack with mocked LLMs; no AWS required |
| Deploy on AWS/RHEL | [AWS provisioning](aws.md) → [coordinated installation](service-orchestration.md), or [individual steps](headless.md) | Three separate hosts; optional RDS |
| Verify security | [Security acceptance](security-acceptance.md) | Tested boundaries, pending checks and evidence requirements |
| Find URLs and credential commands | [Access summary](service-orchestration.md#open-dashboards-and-retrieve-one-credential) | No credentials printed; selected retrieval commands over verified SSH |
| Generate and inspect traffic | [Benchmark](benchmark.md) → [report](report.md) | Six phases, accounting reconciliation and resource graphs |
| Switch to real providers | [Replace mock profiles](providers.md#convert-one-profile-to-a-real-provider) | Keep users, budgets and history; replace selected upstream routes and credentials |
| Remove resources | [Cleanup](cleanup.md) | Inventory and ordered manual teardown |

## Configure each component

| Component | Guides |
| --- | --- |
| Shared trust and connectivity | [Certificates](certificates.md), [peer network rules](network.md) |
| Identity node | [Keycloak installation](keycloak-rhel.md), [gateway OIDC and identity mapping](oidc.md), [synthetic users](keycloak-users.md) |
| Observability node | [Receiver, stores and Perses](observability-rhel.md) |
| Gateway node | [Headless application installer](headless.md), [local collector agent and host sampler](gateway-telemetry.md) |
| Providers and model selection | [Mock profiles and real-provider replacement](providers.md) |
| Storage lifecycle | [Retention defaults and acceptance](storage.md) |
| External database | [Optional private RDS](rds.md) |
| Script entry points and tests | [Categorized script index](../../../tests/external-services/README.md) |

Auxiliary installers live under `tests/external-services`. The normal gateway installer consumes protected external-service configuration; it does not provision AWS or install the auxiliary nodes. All scripts accept explicit inputs; AWS billable operations retain separate administrator confirmation.

## From testing to real providers

1. Finish the mock benchmark, check its cleanup result and save its report.
2. On the gateway, [replace the selected mock profiles](providers.md#convert-one-profile-to-a-real-provider) with reviewed HTTPS endpoints and real credentials.
3. Check the offered models and effective prices, refresh the client configuration,
   and make one small authenticated request. Check its usage in PriceTag.

Provider replacement does not reinstall the gateway, Keycloak, observability or
databases. It preserves synthetic users and historical mock usage, so a fresh
deployment is preferable when you need clean production records. Once any real
provider is configured, the mock benchmark must refuse to run.

For production from the start, use the [one-node quickstart](../../quickstarts/remote-gateway-pricetag/README.md)
or [full deployment with remote services](../../quickstarts/remote-gateway-pricetag/full-deployment.md).
Neither requires synthetic accounts or a benchmark run.

## Provision the three VMs

Already have fresh RHEL hosts? Skip AWS and follow [headless installation](headless.md).
Otherwise use the separate [AWS three-node infrastructure guide](aws.md). It covers
account discovery, deployment manifests, launch summaries and recovery journals.
The [one-node AWS guide](../aws-pricetag.md) is unchanged.

The existing three-VM helper requires an approved subnet. The new
[combined network/host command](infrastructure.md) creates a dedicated VPC and
three restricted hosts, but is not yet the complete deployment flow. Full scoped-role
permissions and automated deletion remain pending. The service coordinator is
implemented; its separate-host RHEL acceptance remains pending.

## Interactive or headless installation

| Deployment | Interactive | Headless |
| --- | --- | --- |
| One node, no SSO/observability | [Main quickstart](../../quickstarts/remote-gateway-pricetag/README.md) | [Installation modes](../../quickstarts/remote-gateway-pricetag/deployment-options.md#one-node-headless) |
| Full lab | Prepare issuer, certificates and observability first; the gateway installer can prompt for provider/user choices | [Full lab installation](headless.md): all service installers accept explicit inputs |

There is no combined interactive wizard for all three nodes. Auxiliary service
installers are command-driven. The full lab uses the source-pinned PriceTag build
for its qualified local SSO lifecycle; published mode is not qualified for that flow.

## Implementation and validation order

1. Provision VMs; verify RHEL, disk, architecture and SSH fingerprints.
2. Prepare role-specific certificates, restrict peer-network rules and qualify the Keycloak installer.
3. Qualify observability ingestion, storage, Perses and OIDC authorization on its dedicated host.
4. Configure the gateway to use existing external services; verify identity mappings and telemetry privacy.
5. Run denial/security tests before the mock traffic sequence; validate dashboards with real emitted signals.
6. Optionally provision private RDS and repeat database failure/recovery tests.

Source-level/cloud-free checks for this draft:

```console
python3 tests/aws/auxiliary.py
python3 tests/aws/plan.py
python3 tests/aws/session.py
python3 -m unittest discover -s tests/external-services -p 'test_*.py'
```
