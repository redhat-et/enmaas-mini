# Coordinated service installation

**Under qualification.** This command runs the existing component installers on
three fresh RHEL hosts. It distributes the selected source revision itself.
It configures dashboard personas and the selected synthetic user population.
SSH host-key verification and the remote benchmark remain separate; the complete
AWS/RHEL flow is not yet qualified. Start with the offline preview.

Service installation needs no AWS credentials. The command runs on the operator workstation and
uses SSH with verified host keys and noninteractive `sudo` on the selected hosts.
It installs mock providers; use [provider replacement](providers.md) after testing.
Production installation remains in the [full deployment quickstart](../../quickstarts/remote-gateway-pricetag/full-deployment.md).

## Prepare the hosts

Before applying:

- Provision three distinct RHEL hosts and apply the [private peer rules](network.md).
- Commit, review and publish the coordinator checkout to the selected public
  GitHub repository. The local checkout must be clean and match `revision`. The
  runner installs Git on the hosts and fetches that exact commit with depth one;
  branch names and tags are not accepted. Python 3 and noninteractive `sudo` must
  already be available on each host.
- Verify SSH fingerprints through a trusted channel. Store the trusted host keys
  and SSH key in user-owned, mode-`0600` regular files. Unlock the key if necessary.
- Choose an issuer DNS name that browsers can resolve to the issuer host. The
  gateway and observability use its private address for server-side OIDC traffic.
  The runner does not create DNS records.
- Choose a new workstation directory for [role-specific certificate bundles](certificates.md).
  The runner generates them there, including the issuer DNS SAN and each host's
  public/private IP SANs. It transfers only each role's allowlisted files. Install
  OpenSSL on the workstation first; keep CA signing keys on that workstation.
- For external databases, prepare the protected application URL and CA files
  documented in [RDS setup](rds.md) on the gateway first.

Use fresh hosts for the initial application. Existing application configuration,
input directories or TLS bundles cause preflight rejection. Do not run the manual
component installation and this command against the same fresh deployment.

## Generate configuration from AWS records

After applying private peer rules, the read-only infrastructure inspector can
fill the host addresses, workstation CIDR, deployment ID and SSH key path.
This step uses the scoped operator AWS profile recorded in `MANIFEST`; service
installation afterward uses only SSH.

Set `ISSUER_NAME` to the selected browser-resolvable DNS name, `KNOWN_HOSTS` to
the verified host-key file, and `LAB_TLS` to a new workstation certificate-directory path.
Set `SOURCE_REPOSITORY` to the public upstream or fork URL containing your reviewed
commit. Then run from that clean checkout:

```sh
SERVICE_DIR="$PWD/.state/services-$RUN_PREFIX"
(umask 077 && mkdir "$SERVICE_DIR") &&
export SERVICE_CONFIG="$SERVICE_DIR/config.json" &&
export SERVICE_STATE="$SERVICE_DIR/receipts" &&
python3 tests/external-services/infrastructure-info.py \
  --manifest "$MANIFEST" --journal "$INFRA_JOURNAL" \
  --service-config "$SERVICE_CONFIG" --issuer-name "$ISSUER_NAME" \
  --known-hosts "$KNOWN_HOSTS" --tls-directory "$LAB_TLS" \
  --revision "$(git rev-parse HEAD)" --source-repository "$SOURCE_REPOSITORY" \
  --population-count 20
```

The first qualification run uses 20 synthetic users above. `--population-count`
accepts 1–10,000 and defaults to 10,000 when omitted. This creates issuer accounts
and matching PriceTag identities, verifies ordinary user roles, assigns a $5
monthly USD override and saves a finite 24-hour key per identity on the gateway.
Separately, the issuer's three demo personas are imported by immutable UUID:
Super admin receives `super_admin`, Alice receives the manager role (`admin`),
and Bob receives `user` with Alice as his manager. Alice and Bob each receive a
$5 monthly override. This step does not issue additional persona keys; browser
access uses their Keycloak credentials. The SSH bootstrap administrator remains
available separately.

Add `--external-database` only after preparing the gateway's protected database
files. The inspector makes read-only AWS calls and creates the local config with
mode `0600`, without overwriting an existing file. It rejects missing private peer
rules, foreign resources, unexpected ingress and inconsistent workstation CIDRs.
It does not establish SSH trust, verify public DNS delegation, copy certificates
or install services. Continue with [preview and apply](#preview-and-apply).

## Configuration for other infrastructure

Create a private directory and a mode-`0600` JSON file within it. Replace the
example values with the selected deployment's addresses and paths. `revision`
must be the full reviewed Git commit, identical on all hosts; obtain it with
`git rev-parse HEAD` in that checkout.

```json
{
  "version": 1,
  "deployment": "em-lab-abcdef123456",
  "revision": "REPLACE_WITH_FULL_40_CHARACTER_GIT_COMMIT",
  "repository": "/opt/enmaas-mini/em-lab-abcdef123456",
  "source_repository": "https://github.com/redhat-et/enmaas-mini.git",
  "ssh_key": "/absolute/private/path/lab-key",
  "known_hosts": "/absolute/private/path/known-hosts",
  "tls_directory": "/absolute/private/path/lab-tls",
  "issuer_name": "identity.lab.example.com",
  "workstation_cidr": "192.0.2.8/32",
  "external_database": false,
  "population_count": 20,
  "hosts": {
    "keycloak": {"ssh": "ec2-user@192.0.2.10", "private_ip": "10.42.0.10"},
    "observability": {"ssh": "ec2-user@192.0.2.11", "private_ip": "10.42.0.11"},
    "gateway": {"ssh": "ec2-user@192.0.2.12", "private_ip": "10.42.0.12"}
  }
}
```

Use the deployment ID from the infrastructure manifest. `repository` must be
`/opt/enmaas-mini/` followed by that ID. Use the upstream URL for merged work or
your public fork URL for a reviewed, published test commit. Only HTTPS GitHub
URLs ending in `/enmaas-mini.git`, without embedded credentials, are accepted.
The configuration contains
paths, not credential values. Paths must be absolute and contain no shell expansion
or traversal components. Keep CA signing keys in the workstation's authorities
directory; that directory is never transferred.

`external_database: true` selects the existing gateway files
`/root/pricetag-external-db/maas.url`, `metering.url` and `ca.crt`.
It does not create RDS, accounts or database roles.

## Preview and apply

Run from the reviewed repository on the workstation. Set `SERVICE_CONFIG` to the
private JSON file and `SERVICE_STATE` to a new receipt directory beneath an
existing private parent directory.

```sh
python3 tests/external-services/services.py --config "$SERVICE_CONFIG"
```

The preview is offline: it does not read certificate contents, contact SSH hosts,
change files or call AWS. Review its ordered commands before applying:

```sh
python3 tests/external-services/services.py \
  --config "$SERVICE_CONFIG" --state "$SERVICE_STATE" \
  --confirm "install $RUN_PREFIX"
```

The runner checks all three hosts before the first installation step. It then
generates the private test CA and role certificates on the workstation,
installs prerequisites, creates root-owned checkouts of the exact commit,
transfers TLS bundles, starts Keycloak, transfers the two
separate OIDC client files, installs observability, installs the mock gateway and
connects its telemetry agent. It then creates the synthetic issuer population,
exports UUIDs/display attributes without passwords, transfers that export and
imports matching PriceTag identities through the private partner API. It verifies
their ordinary user roles, sets $5 monthly budgets and saves finite 24-hour keys
privately on the gateway. It uses the source revisions pinned by the reviewed
gateway installer. It never transfers the issuer administrator credential to the
other hosts.

Checkout creation refuses an existing destination. Fetches are noninteractive,
with Git hooks and credential helpers disabled. Local untracked files and other
local branches are not uploaded. Failed checkouts remain for inspection.

The certificate directory must not already exist. A completed generation step
is recorded; resume reuses those files rather than rotating keys. The private
CA must be trusted explicitly by test browsers and clients. These 30-day leaf
certificates have no automatic renewal; see [certificate scope](certificates.md).

Progress prints step names only. Remote stdout/stderr are suppressed because
installer failures can include sensitive diagnostics. Inspect the affected host
through SSH when a step fails. Keep credentials out of copied logs.

## Open dashboards and retrieve one credential

Successful installation prints an access summary. Reprint it from the workstation
without connecting to AWS or the hosts:

```sh
python3 tests/external-services/services.py --config "$SERVICE_CONFIG" --access
```

The summary includes:

- PriceTag login, Perses and issuer URLs, plus the workstation's public CA path.
- Verified-SSH command lines for the three personas' saved Keycloak passwords.
- Commands to inspect or explicitly export population user 1's inference key,
  and retrieve that user's browser password. Change `--index 1` to another index.
- The separate bootstrap administrator key command, provider inventory, and the
  six-phase mock benchmark command (ten minutes per phase).
- Interactive provider-replacement commands bound to this gateway and checkout.
  These allocate a terminal for hidden key prompts; they do not contain keys.

The summary contains **no credential values**. Running a password/key export
command prints the selected secret: pipe it directly to a clipboard tool or
private file rather than displaying it in a recorded terminal. Metadata commands
print only paths, identity and expiry. These are saved initial credentials;
Keycloak password changes and key expiry/revocation still apply.

The summary describes configured addresses, not current readiness. Trust the
selected private CA explicitly before browser access; do not disable TLS
verification. The issuer DNS name must resolve on your workstation. Observe the
[benchmark instructions](benchmark.md) before running the printed command; it
refuses real-provider deployments. [Replace mocks with real providers](providers.md)
only after stopping the benchmark and saving its report.

## Recovery and acceptance

The private receipt directory binds the exact configuration and generated plan.
Each step is recorded before execution and marked complete only after success.
Completed steps are skipped on a repeat invocation. Changed configuration,
noncontiguous receipts or a started-but-unconfirmed step stop the run.
Fresh-host checks remain enabled separately for each host until its TLS transfer
is recorded complete. Completing local certificate generation or preparing another
host does not allow an existing deployment to be adopted.

A timeout or broken SSH connection does not prove the remote operation stopped.
Inspect the host and preserve receipts; do not remove them to force a retry.
There is no automatic rollback, deletion or adoption of unknown state. Completed
receipts record past execution, not continuing service health.

Successful execution reports `dashboard_personas_configured: true` only after
role, team and budget readback. `acceptance_verified` remains `false`: completion
of setup does not prove browser access or cross-host security. Persona setup
refuses conflicting identities, unexpected budget overrides and uncertain writes;
its private receipts are in `/root/enmaas-personas`. Keep the [population records](keycloak-users.md) for
verification and retirement. Run the [security acceptance checks](security-acceptance.md)
and [mock benchmark](benchmark.md), then inspect the dashboards. Separate-host
RHEL, reboot behavior and the complete installation remain unqualified until
those checks pass on the supplied infrastructure.

## Retrieve access instructions for a saved deployment

From the workstation checkout, use the manifest and service configuration saved
for the same deployment:

```sh
python3 tests/external-services/deployments.py list
python3 tests/external-services/deployments.py access "$MANIFEST" \
  --service-config "$SERVICE_CONFIG"
```

The command checks that the deployment ID and SSH key match, then prints configured
URLs, the public CA path and SSH commands to retrieve one selected credential.
It reads only the private manifest/configuration files. It does not retrieve
passwords or keys, contact AWS/hosts, or certify service readiness. Re-run live
infrastructure verification before using a stale saved configuration. Execute a
printed credential command only in your private terminal when you need it.

## Issuer DNS preflight

Before applying, make the configured `issuer_name` resolve to the Keycloak host's
public IPv4 from the workstation/browser. The coordinator checks this before
creating receipts or changing any host. Missing/wrong answers, additional IPv4
addresses or IPv6 answers are rejected by this IPv4-only three-host workflow.
Preview and access-summary commands remain offline.

A workstation hosts-file mapping can support the initial private-CA test; other
users need their own mapping or working DNS. This check does not establish public
DNS delegation, DNSSEC, browser-specific DNS behavior or global propagation.
TLS still verifies the canonical issuer name. Gateway and observability OIDC
clients retain their exact private-IP mapping, so their backchannels do not use
the public address. Verify both actual paths during RHEL acceptance.
