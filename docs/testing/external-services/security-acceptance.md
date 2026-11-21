# Deployment security qualification

Local containers and mocked AWS tests validate specific contracts. They do not
qualify separate-host RHEL, live IAM permissions or unrestricted public access.
Keep the deployment restricted until its own allowed/denied-path checks pass.

## Tests and their limits

| Check | Evidence | Separate deployment acceptance |
| --- | --- | --- |
| AWS and installer contracts | `aws-mocked.yml`; mocked responses and coverage of its declared modules | Live IAM deny probes, ownership, routing, security groups and cleanup |
| Identity boundary | `external-identity.yml`; real pinned issuer routes, forwarding-header and lockout checks | RHEL PostgreSQL lifecycle, host confinement, restart and actual source restrictions |
| Telemetry boundary | `external-observability.yml`; real OIDC/group gates, mTLS rejection, redaction, bounded payloads and store queries | Cross-host private paths, certificate lifecycle, actual-producer privacy and retention over time |
| Gateway integration | Explicit `local-stack.yml` runtime; source-pinned applications, browser/API tests and accounting | Target RHEL SELinux/firewall, reboot, real provider credentials and operational recovery |
| Streaming benchmark | Explicit benchmark mode; bounded mock load, exact ledger reconciliation, recovery and dashboard queries | Sustained cloud workload, separate generator, representative retained data and supported capacity |

See [local qualification](local-podman.md), the [test entry points](../../../tests/external-services/README.md)
and the [declared coverage scope](../../../tests/aws/README.md). A green unit job
is not a completed runtime qualification. Temporary test cleanup is also checked;
forced host shutdown still requires inspection of owned resources before reuse.

## Public access and administrator authentication

Only reviewed client-facing gateway routes, issuer realm/resource routes and
authenticated observability pages may become public. Database, query, raw
management and collector ingestion interfaces stay private. Use canonical issuer
DNS with private peer resolution for the three-host layout; the older public-IP
issuer path does not satisfy private-only backchannel requirements.

**SSO alone does not enforce administrator MFA.** The pinned PriceTag
[login handler](https://github.com/Ladas/pricetag-metering/blob/2281e1a84bfc3b998e7db36bc01bae88130bfcdd/internal/handler/auth.go)
also accepts MaaS keys for browser login. Keycloak OTP would protect only the
OIDC path. Before claiming MFA protection, enforce the intended policy on direct
key login, management APIs and existing sessions, then test enrollment, recovery,
role promotion and revocation. Hiding a login form is insufficient. Preserve a
restricted SSH recovery path. These controls are not provided by this test realm,
which uses password login with lockout protection.

## Acceptance sequence

1. **Workstation:** inspect account, deployment tags, DNS, addresses, peer rules
   and optional RDS TLS settings. Keep AWS credentials off service hosts.
2. **Every host:** verify SSH fingerprints, RHEL version, SELinux enforcing,
   service identities, published sockets, firewall state and certificate trust.
3. **Allowed and denied clients:** test valid TLS/login/inference, wrong CA or
   client certificate, anonymous and lower-role access, public administration
   rejection and private-only backend reachability. A timeout alone does not
   prove authorization rejection.
4. **Traffic and failure:** reconcile accounting during mock load; test telemetry
   receiver outages, recovery, queue/drop behavior, retention and service reboot.
   Telemetry and billing are separate paths.
5. **Retirement:** complete [test-user offboarding](keycloak-users.md), inspect
   [owned resources](cleanup.md) and retain failures as open checks.

Record revision/digests, host role, expected/actual outcome and reviewed evidence.
Do not publish state directories, container environments, keys, provider URLs or
database connection strings. Share redacted summaries and measurements only.
