# Security qualification

**Status:** local functional qualification is complete for the recorded scenarios; AWS/RHEL qualification is pending deployed infrastructure. Passing local tests or CI does not certify the AWS deployment. Keep this lab restricted until its deployment-specific checks pass.

## Evidence and remaining work

The [isolated runtime suite](../../../tests/external-services/README.md#qualification-tests-and-shared-support)
checks real pinned issuer/proxy and collector containers. CI runs it without cloud
credentials; unit tests run separately in the validation workflow. Neither suite
qualifies the three-host network, RHEL service confinement or a public deployment.

| Boundary | Evidence available locally | AWS/RHEL acceptance still required |
| --- | --- | --- |
| Host and network exposure | Loopback-only Podman ports; generated peer rules and AWS checks tested with simulated responses | Inspect live account/resource ownership, public IPs, security groups and IPv6 rules. Probe from an allowed workstation, an unauthorized source, gateway and auxiliary hosts. Only intended HTTPS/SSH/telemetry paths may succeed. |
| TLS and machine identity | Verified hostname/CA, missing client-certificate TLS rejection (HTTP denial, timeout and refused connection cannot pass), unrelated-CA client rejection and wrong server-CA rejection, accepted issued certificate before/after; generated role bundles | Verify actual SANs, certificate chain/expiry, trust on every client and file ownership. Reject wrong CA/client certificate. Keep both signing keys on the workstation. Test replacement without weakening validation. |
| Browser SSO | Immutable identity, unknown/inactive-user denial, session deactivation, current-role changes, confirmed logout | Repeat against actual issuer IP/callbacks across hosts. Check admin recovery, group removal and revoked/expired sessions. PriceTag roles and observability groups require separate explicit assignment. |
| Observability authorization | Anonymous/member/manager denial; explicit admin group; existing-session group removal denied after refresh | Verify no direct Perses, Prometheus, Loki, Tempo or Keycloak admin bypass from workstation/VPC peers. Access remains admin-only, not tenant-isolated observability. |
| Service privileges | Rootless fixture, capability restrictions, generated Quadlets; actual local systemd sampler/IP-policy test | Verify RHEL service identities, mounts, capabilities, secrets, SELinux enforcing, firewalld, boot/restart and unit sandboxing. Local Podman does not establish these properties. |
| Telemetry privacy | Seeded sensitive fields removed; original log bodies replaced; browser queries work; 30-second local receiver outage preserves mock inference/accounting and fresh synthetic signals and actual gateway metrics/traces recover without a gateway restart | Inspect representative real producer fields before accepting real traffic. Repeat outage across hosts and verify gateway exporter recovery, queue/drop behavior and continued inference/accounting. Filtering is an allowlist, not universal secret detection. |
| Storage | Configured retention and memory bounds; log restart/truncation test | Measure elapsed retention deletion, disk pressure and recovery. Loki/Tempo disk quotas and lossless log delivery are not qualified. Confirm encrypted volumes and backup/cleanup decisions. |
| Telemetry request bounds | Pinned collector rejects plain and gzip payloads exceeding 4 MiB; normal signals export afterward; generated export batches cap at 512 records | Confirm installed settings, producer compatibility and peak-load behavior. These limits do not establish per-producer quotas or bound label cardinality over time. |
| Local database separation | The main Podman fixture deliberately shares its bootstrap database role; a separate PostgreSQL test exercises least-privilege roles | Verify actual gateway roles are non-superuser and cannot access the other service database. Keycloak must keep its separate private role/database. The main local fixture cannot establish this boundary. |
| AWS lifecycle helpers | Registry selection clears stale credentials/SSH targets; simulated DNS exact-record ownership and EC2/RDS/network-dependency inventory tests | Qualify scoped IAM, DNS delegation, interrupted creation/recovery and cleanup in the actual account. Metadata checks do not authorize deletion; full cleanup executor is pending. |
| Issuer login protection | Disposable pinned Keycloak test: valid login, five failed logins, temporary rejection of valid credentials and recovery | Apply and repeat with the RHEL/PostgreSQL issuer. Existing local demo realm was not retroactively changed. Administrative MFA, recovery and public-route review remain required. |
| Optional RDS | Plan/create/verify simulated AWS tests; local PostgreSQL TLS/SCRAM and role separation | Inspect live private subnets/SG/encryption/TLS parameters and deletion protection. Test actual schema migrations, reconnect/restart, least privilege and gateway behavior during DB failure. |
| Mock benchmark | Six ten-minute local phases, per-user token/USD reconciliation, finite test keys, cleanup and fresh telemetry | Run full ten-minute phases across hosts, measure resource headroom and generator overhead. Capture failures and verify cleanup; do not infer multi-host capacity from the local fast-response mocks. |

## Public-access gate

The current lab is not qualified for unrestricted public deployment. Only reviewed
client-facing API and authenticated browser routes may become public. Raw service
APIs, databases and telemetry ingestion must remain private. Public IPs with an
explicitly trusted private CA are the current test mode; DNS and publicly trusted
certificates are separate configuration choices.

Remaining gates include actual RHEL firewall/SELinux/reboot checks, AWS deny probes,
private identity backchannels, certificate lifecycle and representative real
telemetry privacy review. The current IP-based OIDC issuer uses public-IP peer
traffic; it does not yet satisfy the private-only backchannel requirement.

## Run the checks in order

### Administrator MFA must cover every login path

Keycloak OTP would protect the OIDC path only. The pinned PriceTag login handler
also accepts a MaaS key at `POST /login` and creates a browser session independently
of Keycloak. Enabling an SSO button does not disable this path. See the
[pinned login handler](https://github.com/Ladas/pricetag-metering/blob/2281e1a84bfc3b998e7db36bc01bae88130bfcdd/internal/handler/auth.go#L153).

Before claiming administrator MFA protection:

- Define whether public browser administration requires SSO, while keeping
  inference-key authentication available for harnesses.
- Enforce that policy on every browser/API management entry point, including
  direct key login and existing sessions. Hiding the key form is insufficient.
- Qualify mandatory enrollment, later logins, recovery, role promotion and session
  expiry/revocation. A first-login enrollment flag alone does not establish that
  future administrator sessions meet the policy.
- Preserve a restricted SSH recovery path and test it without exposing the
  Keycloak administrator API publicly.

These controls are not implemented or qualified by the current lab. Its generated
realm still uses password login with lockout protection. Do not describe it as
MFA-protected or enable unrestricted public administration on that assumption.

### Deployment checks

1. **Administrator workstation:** deploy with the [AWS helper](README.md#provision-the-three-vms). Keep credentials here. Run `aws_lab_info`, review the [peer-rule plan](network.md) and optional [RDS verifier](rds.md). These inspect configuration; they do not prove packet-level isolation.
2. **Each VM:** confirm the intended host fingerprint, RHEL version and SELinux enforcing. Install the reviewed configurations using the [headless runbook](headless.md). Record service identity, unit state, published sockets and firewall state without dumping secret environment variables.
3. **Before traffic:** verify TLS and both allowed and denied network paths. Repeat browser role/session and collector client-authentication tests. A timeout alone is not proof of application authorization.
4. **Under mock traffic:** run the [benchmark](benchmark.md), inspect [Perses and PriceTag](local-dashboards.md) and reconcile accounting. Simulate receiver failure separately; preserve the original configuration and verify recovery.
5. **Afterward:** collect redacted evidence, confirm test-account/key retirement and use the [cleanup inventory](cleanup.md). Keep failed checks open; never waive them based on green CI.

## Safe evidence collection

Record test ID, tested revision/digest, host role, timestamp, expected result, actual result and evidence path. Do not publish whole state directories, container environment dumps, JWT/key material, provider URLs/credentials or database connection strings. Share only reviewed configuration summaries and measurements.

AWS administrator credentials are neither needed nor accepted by the application installers. The operator can run AWS checks and supply their nonsecret output; SSH-based host and application tests can then run against the supplied lab hosts. No AWS qualification has been performed until those resources exist and these results are recorded.
