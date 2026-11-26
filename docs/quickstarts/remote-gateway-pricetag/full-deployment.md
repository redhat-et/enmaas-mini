# Gateway with external identity and observability

Use the same RHEL gateway installer and provider-management commands as the
[one-node quickstart](README.md). Add external service inputs before installation;
do not put real credentials into the disposable local Podman fixture.

## Prepare the services

| Service | Required contract | Setup |
| --- | --- | --- |
| OIDC issuer | HTTPS discovery, confidential client, exact callback, stable issuer identity | [Gateway OIDC and supplied TLS](external-auth.md) |
| Gateway HTTPS | Server certificate/key matching its client and health-check names | [Supplied TLS bundle](external-auth.md) |
| Observability | Private OTLP ingestion with role-specific client certificates; authenticated dashboards | [Observability service and gateway relay](../../testing/external-services/observability-rhel.md) |
| PostgreSQL | Local by default; separate MaaS and metering credentials for external databases | [External PostgreSQL](external-database.md) |

Keep databases, ingestion and management interfaces private. Only client-facing
HTTPS and source-restricted SSH should be externally reachable. Trust the public
CA explicitly when using private certificates; keep CA signing keys off hosts.
Sharing an issuer does not synchronize PriceTag roles and observability groups.

## Install and configure

1. Prepare RHEL and copy the reviewed checkout using the [quickstart](README.md).
2. Prepare the protected issuer and certificate inputs from
   [external authentication](external-auth.md). Use its explicit compatible
   metering source revision; the default published image is not assumed to
   support this OIDC flow.
3. Run the gateway installer with those inputs. Use hidden provider prompts, or
   [headless mode](headless.md) with private key files. Configure real providers
   using [provider management](providers.md).
4. Register immutable issuer identities with the documented SSH-admin helper.
   Validate browser login and each role; HTTPS readiness alone does not prove
   that issuer discovery or SSO succeeded.
5. Install the [gateway telemetry relay](../../testing/external-services/observability-rhel.md)
   with its own client certificate. Verify metrics, logs and inference traces in
   the [monitoring dashboards](../../testing/external-services/monitoring.md).
6. Issue an inference key and configure [your client](clients.md). Browser SSO
   credentials and inference keys are separate.

Existing compliant external services can be used. The dedicated Keycloak and
observability installers under `tests/` are evaluation tooling, not a qualified
production identity/observability lifecycle manager.

## Test the complete layout

For fresh dedicated hosts, the [three-host coordinator](../../testing/external-services/three-host-install.md)
installs a mock gateway, test users and remote services from one protected
configuration. Review its offline plan before applying. It creates no cloud
resources and refuses pre-existing deployment state.

Before sharing access, validate role boundaries, authenticated inference,
private ingestion, populated telemetry, host firewall/SELinux and restart
behavior. Local checks do not establish separate-host RHEL or unrestricted
public-access readiness. The coordinator records that acceptance remains open. Use the
[deployment security checklist](../../testing/external-services/security-acceptance.md),
including the separate administrator-MFA gap.
