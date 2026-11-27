# Dedicated network and three RHEL hosts

This testing helper combines network creation and three host launches. It uses
an existing [deployment manifest](aws.md) and scoped operator profile. The one-node
AWS helper is unchanged.

**Status:** mocked orchestration tests pass. Live AWS permissions, recovery and
RHEL acceptance remain unqualified. This is not yet the complete four-command
setup: peer rules, certificates, services, optional RDS/DNS and cleanup are separate.
All three hosts start with SSH and HTTPS restricted to the operator's IPv4 `/32`.

## Preview a new deployment without AWS calls

Run from the checkout on the operator workstation. Use the group-scoped operator
profile created by [access bootstrap](operator-access.md), the same approved
`RHEL_AMI_ID`, and an
existing SSH key with its RSA/Ed25519 `.pub` sibling. No private key contents are
read or sent to AWS by this command.

DNS is optional. Start with an empty argument list; when using an existing public
zone, replace it with the paired arguments below. Obtain the values from the
[read-only discovery](aws.md) results; use its `--select-zone` option to print
the paired exports. Confirm permission to use the zone.
The scoped role's DNS policy must cover the same zone/domain.

```sh
DNS_ARGS=()
```

For a selected zone:

```sh
DNS_ARGS=(--dns-zone-id "$ZONE_ID" --dns-domain "$DNS_DOMAIN")
```

This records DNS scope for later record creation. It does not select publicly
trusted certificates; the current service setup still uses explicitly trusted
private CAs. Account-wide zone listing belongs to administrator discovery, not
the narrowly scoped deployment role.

```sh
INFRA_JOURNAL="$PWD/.state/deployments/launch-$(date -u +%Y%m%dT%H%M%SZ)"
python3 tests/external-services/infrastructure.py \
  --group "$DEPLOYMENT_GROUP" --account "$ACCOUNT" \
  --region "$REGION" --profile "$AWS_PROFILE" --ssh-key "$SSH_KEY" "${DNS_ARGS[@]}" \
  --image "$RHEL_AMI_ID" --cidr 10.42.0.0/20 \
  --zone "${REGION}a" --zone "${REGION}b" \
  --allowed-cidr "$ALLOWED_CIDR" --journal "$INFRA_JOURNAL"
```

Region defaults to `eu-central-1` if omitted. Choose a nonoverlapping private CIDR
and available zones; these example placements do not establish capacity. Live
preflight checks account, zones and owned regional VPC CIDRs, but cannot discover
every on-premises or peered range. Preview creates neither a registry entry nor
AWS resources. Its proposed random run ID is regenerated when applying.

## Create the infrastructure

Use the same inputs and journal path, adding explicit group/account confirmation:

```sh
python3 tests/external-services/infrastructure.py \
  --group "$DEPLOYMENT_GROUP" --account "$ACCOUNT" \
  --region "$REGION" --profile "$AWS_PROFILE" --ssh-key "$SSH_KEY" "${DNS_ARGS[@]}" \
  --image "$RHEL_AMI_ID" --cidr 10.42.0.0/20 \
  --zone "${REGION}a" --zone "${REGION}b" \
  --allowed-cidr "$ALLOWED_CIDR" --journal "$INFRA_JOURNAL" \
  --apply --confirm "deploy $DEPLOYMENT_GROUP $ACCOUNT"
```

The command registers the new deployment before cloud operations and prints its
recovery-manifest path. Keep that path as `MANIFEST` for inspection and subsequent
setup. If creation fails, retain the same journal and manifest; do not repeat new
deployment mode to bypass a partial failure.

This is billable. It creates one VPC, service/database subnets across the chosen
zones, an internet gateway and route tables, followed by:

| Host | Instance | Encrypted root disk |
| --- | --- | --- |
| Gateway | `m7i.2xlarge` | 150 GiB |
| Keycloak | `m7i.large` | 50 GiB |
| Observability | `m7i.xlarge` | 100 GiB |

Service hosts get public IPs explicitly. Database subnets have no internet-gateway
route. An IP or accepted launch is not service readiness: the output keeps
`runtime_verified` and `services_installed` false.

## Inspect running hosts

After boot, query the deployment using the same operator profile and journals:

```sh
python3 tests/external-services/infrastructure-info.py \
  --manifest "$MANIFEST" --journal "$INFRA_JOURNAL"
```

The summary includes each host's public/private IP, SSH login, SSH key path,
HTTPS base URL and recovery journal. It does not read credentials or private SSH
key contents. Pending hosts, mismatched ownership/VPCs and unexpected ingress
stop inspection; wait for boot or resolve the reported mismatch before continuing.

It also lists missing private peer rules without applying them. Host addresses
are not proof of application availability: SSH host fingerprints, service TLS,
SSO, dashboards and inference still need installation and acceptance checks.

## Prepare the service configuration

After applying the private peer rules below, use the inspector's
[service-config handoff](three-host-install.md).
It fills addresses and the workstation CIDR from verified records, writes a
private config without overwriting existing files, and leaves DNS and SSH trust
as explicit operator checks. Service installation then needs SSH, not AWS credentials.

After verifying SSH fingerprints, choose a new private state directory and a
DNS name for the issuer. Use the repository URL in which your selected commit
is published; a fork URL is supported.

```sh
LAB_STATE="$PWD/.state/services-$RUN_PREFIX"
mkdir -m 700 "$LAB_STATE" &&
python3 tests/external-services/infrastructure-info.py \
  --manifest "$MANIFEST" --journal "$INFRA_JOURNAL" \
  --service-config "$LAB_STATE/services.json" \
  --issuer-name "$ISSUER_NAME" --known-hosts "$KNOWN_HOSTS" \
  --tls-directory "$LAB_STATE/tls" --revision "$(git rev-parse HEAD)" \
  --source-repository https://github.com/redhat-et/enmaas-mini.git \
  --population-count 20
```

This queries AWS and writes a protected configuration; it does not install
services, create DNS records, trust SSH fingerprints or issue certificates.
Peer rules must already pass inspection. Use that file in the three-host guide's
preview/apply steps, skipping its manual configuration creation.

## Recover without losing ownership records

The command creates the journal directory exclusively and will not reuse it.
It validates all three host plans before the first host launch. If an operation
fails, it stops without deleting resources or launching subsequent hosts.

- `network/`: durable network intent/result records.
- `<run>-gateway.json`, `<run>-keycloak.json`, `<run>-observability.json`:
  existing VM recovery format and filenames, consumable by the peer-network helper
  using `--state-dir "$INFRA_JOURNAL"`.
- Numbered JSON records: combined plan and host launch progress.

Do not bypass a failure with a new directory or deployment ID. Inspect the
journals and actual tagged resources first. Use the [network journal inspector](aws.md)
and [cleanup inventory](cleanup.md); neither automatically deletes this deployment.
Service installation follows the [three-host guide](three-host-install.md). Benchmark qualification is separate.

## Use an already registered new deployment

For a registry entry that has not launched resources yet, replace `--group`,
`--account`, `--region`, `--profile` and `--ssh-key` with `--manifest "$MANIFEST"`.
Preview remains the default; applying this mode requires
`--apply --confirm "deploy $RUN_PREFIX"`, where `RUN_PREFIX` is the manifest's ID.
Account, region, profile, key and DNS scope are read from that manifest and
cannot be overridden; omit `DNS_ARGS` when using `--manifest`.
This mode does not resume an interrupted network journal.

## Peer networking after launch

VM launch does not grant service-to-service access. Once all hosts are running,
preview the missing rules with `infrastructure-info.py` as above. Apply them using
the account, region and run ID from the selected manifest:

```sh
python3 tests/external-services/infrastructure-info.py \
  --manifest "$MANIFEST" --journal "$INFRA_JOURNAL" \
  --apply-peers --confirm "grant peers $ACCOUNT $REGION $RUN_PREFIX"
```

The inspector checks the completed network journal, host/security-group ownership,
restricted original ingress and private peer addresses twice before writing. It
adds only missing security-group references: gateway and observability to identity
TCP 8443, and gateway to observability TCP 24443. It verifies the final rules and
unchanged hosts. No public CIDR, SSH or database rule is added.

After an interrupted grant, rerun the read-only inspection first. The apply path
reads current rules and skips grants already present; it does not replay launches
or delete anything. Concurrent changes cause refusal. This is AWS configuration
verification, not a packet-level connectivity test.

You can add the same `--apply-peers --confirm ...` options to the
[service-config handoff](three-host-install.md)
so configuration is written only after the peer rules are verified. The command
still does not set up DNS, certificates or SSH trust. Private issuer traffic needs
a canonical DNS issuer and the exact private mappings on both clients.

## Optional deployment DNS handoff

Select `zone_id` and `domain` when creating the deployment manifest, and bootstrap
the operator role with the matching [optional DNS grant](operator-access.md#optional-dns-namespace).
Use `infrastructure.py --manifest ...` for that registered deployment. Network/VM
creation records the DNS selection but does not create records before hosts run.

Preview the exact records from the verified running host addresses:

```sh
python3 tests/external-services/infrastructure-info.py \
  --manifest "$MANIFEST" --journal "$INFRA_JOURNAL" \
  --dns-journal "$INFRA_JOURNAL/dns.json"
```

Create only those records after review:

```sh
python3 tests/external-services/infrastructure-info.py \
  --manifest "$MANIFEST" --journal "$INFRA_JOURNAL" \
  --dns-journal "$INFRA_JOURNAL/dns.json" --apply-dns \
  --confirm-dns "create $RUN_PREFIX $ACCOUNT"
```

The handoff rechecks live host identity before creation, then uses the existing
DNS helper's public-zone validation, collision refusal, ownership nonce and
immutable journal. It never uses UPSERT or changes a hosted zone. Retry only after
inspecting an uncertain result: matching journaled records cause no new write;
conflicts stop. Keep the same journal for [DNS cleanup](dns.md).

Use `keycloak.RUN_PREFIX.DOMAIN` as the service configuration's `issuer_name`.
Record creation is not proof of public delegation or propagation. The service
coordinator checks workstation resolution before installation; other browsers
must resolve the same name too. Certificates remain private-CA certificates—DNS
creation does not issue publicly trusted certificates.

The DNS flags can accompany the peer-rule/service-config handoff. Each mutation
keeps its own exact confirmation. If a later step fails, earlier verified grants
or DNS records remain; no automatic rollback deletes infrastructure.

The network preflight requires the deployment group’s assumed operator role.
An administrator profile, IAM user or another group’s role is rejected before
EC2 network calls. This identity check complements the scoped IAM policy; it does
not replace live permission-boundary testing.
