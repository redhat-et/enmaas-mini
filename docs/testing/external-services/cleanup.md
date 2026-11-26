# Manual AWS cleanup

Run cleanup from the AWS administrator workstation or console. The VM/RDS helpers do not delete resources. The separate [DNS helper](dns.md) can delete exact journaled DNS records after explicit confirmation. Keep the launch journals and export sanitized benchmark evidence before deleting anything. Match the account, region, run prefix and resource IDs; names alone are insufficient ownership evidence.

## Read-only inventory for registry-created deployments

### Inspect a dedicated-network creation journal

Before retrying or cleaning up a failed network creation, inspect its local records:

```sh
python3 tests/external-services/vpc-inspect.py \
  --manifest ".state/deployments/$RUN_PREFIX.json" \
  --journal ".state/deployments/$RUN_PREFIX-network"
```

Use the journal path selected during creation. This command is offline: it does
not load AWS credentials or change resources. It reports confirmed resource IDs
and any operation whose outcome is uncertain. Keep that journal intact.

The check validates record structure, sequence and the manifest relationship;
it cannot authenticate a journal against deliberate changes by its owner or prove
current AWS state. Even `creation-recorded-complete` has `live_verified: false`
and `deletion_authorized: false`. Inspect live ownership tags, account, region,
attachments and route associations before recovery. Do not retry an uncertain
write by deleting or renaming the journal. Automated recovery/deletion is pending.

### Inspect the three VM journals against AWS

For a deployment created with the new registry-generated run prefix, the following
command compares its three VM journals with live EC2 resources. Run it in your
AWS operator terminal using the existing profile recorded in the manifest:

```sh
python3 tests/external-services/cleanup-inventory.py \
  ".state/deployments/$RUN_PREFIX.json" --state-dir .state
```

For the combined infrastructure command, include its actual launch journal and
network records:

```sh
python3 tests/external-services/cleanup-inventory.py "$MANIFEST" \
  --state-dir "$INFRA_JOURNAL" --network-journal "$INFRA_JOURNAL/network" \
  --teardown-plan
```

The result includes the dedicated VPC/subnet/route/internet-gateway creation IDs
and uncertain operations, separately from live-verified VM metadata. Its final
network stage requires checking current VPC ownership and all service/interface,
route association, endpoint and gateway dependencies. A complete creation journal
does not establish that the VPC is now empty or safe to delete. Missing network
records remain an explicit unverified stage; no resource is adopted or deleted.

It requires the recorded account, region, role, instance/group/key identifiers,
ownership tags and root-volume relationships to agree. Missing or extra tagged
resources stop the inventory for reconciliation. It refuses unexpected disks,
foreign attachments and changed deletion flags. Only resource metadata is printed. Unconsumed pagination tokens, missing resource
IDs and duplicate IDs stop the inventory; it must not silently truncate or merge
AWS results before ownership checks.

New launches also record the imported SSH key pair’s immutable AWS ID. Retry and
cleanup inventory reject a replacement with the same name but a different ID.
Older journals remain readable, but name/tag matching alone is not sufficient
to authorize automated key-pair deletion. A failed or ambiguous import leaves
`key-import-requested` in the recovery journal and does not launch an instance.

New VM launches tag instances, volumes and their new primary network interfaces
at creation. Recovery journals record that tag contract so retries preserve the
original request. Older journals retain their original instance/volume-only tags;
the helper does not retag or adopt existing interfaces. This uses
[RunInstances creation-time tagging](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RunInstances.html)
and requires the operator's creation-tag permissions to cover network interfaces.
Tags alone never authorize deletion of an interface.

The inventory also queries [network interfaces by security group](https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-network-interfaces.html),
without requiring deployment tags on those interfaces. It reports journaled VM
attachments, service-managed interfaces and unresolved dependencies. Sharing a
security group does **not** establish ownership or authorize interface deletion.
Reconcile service-managed interfaces through their owning service and inspect
unresolved dependencies before removing groups. A partial response or unexpected
account/query scope stops the inventory. Unassociated retained resources still
require separate inspection.

`security_group_references` also lists current-account inbound and outbound rules
referencing a deployment group, using the documented
[security-group reference filters](https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-security-groups.html).
The source group is marked owned only if it is already in the verified deployment
inventory. References from other groups require owner coordination; the helper
never adopts or modifies them. Partial responses, mismatched accounts and results
outside the requested scope stop inventory. Cross-account and peered-VPC references
remain separate checks; an empty list does not prove a group can be deleted.

If this deployment includes RDS, add its private creation journal:

```sh
python3 tests/external-services/cleanup-inventory.py \
  ".state/deployments/$RUN_PREFIX.json" --state-dir .state \
  --rds-journal ".state/$RUN_PREFIX-rds.json"
```

The report includes verified RDS dependency ARNs and the managed-secret reference;
it never reads that secret. The RDS group must be distinct from the VM groups.
The database's immutable resource ID must also match the creation journal; the
inventory prints it for later backup/deletion review. A name/ARN match alone is
insufficient to adopt a replacement database.
Final snapshot and backup-retention choices remain explicit operator decisions.

`retained_storage` lists the original database's same-region RDS snapshots and
automated backups using its immutable ID. Each entry has
`deletion_authorized: false`; metadata matching is not permission to delete data.
Incomplete pagination, duplicate records and mismatched source/account/region
stop inventory. The helper still requires a live, verified database for the full
RDS inventory; after database deletion, reconcile retained data manually using
the preserved resource ID and journal.

Cross-region copies, AWS Backup vault recovery points and final-snapshot choices
are outside this listing. Check them separately. An empty result is not proof
that no retained database data or billable backups exist anywhere in the account.

**No deletion is implemented by this command.** DNS, IAM, retained RDS storage,
network-dependency reconciliation and partial-launch recovery still need the
inventory below.
Older runs without the shared ownership tag use the manual procedure; do not
retag resources just to make the new inventory accept them. Offline tests cover
ownership mismatches; live AWS validation is pending.

## VM termination prototype

`tests/external-services/cleanup-instances.py` provides a separate **VM-only**
termination stage. Preview is read-only. It is not the complete cleanup command:
DNS, RDS/backups, groups, key pairs, VPC resources and local state remain. The
bootstrap role does not yet include termination grants; live AWS qualification
and scoped deletion permissions are pending. Its mocked tests run in the AWS
statement/branch-coverage gate; that does not qualify live deletion.
Do not broaden credentials to bypass
that unfinished integration.

For review, preview with the same deployment manifest and launch journals:

```sh
CLEANUP_JOURNAL="$INFRA_JOURNAL/terminate-vms"
python3 tests/external-services/cleanup-instances.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" \
  --journal "$CLEANUP_JOURNAL"
```

### Review the optional VM-termination policy

Render a policy for the three instance IDs in the creation journals, without
loading AWS credentials or contacting AWS:

```sh
python3 tests/external-services/cleanup-instances.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" --policy
```

An administrator must review this policy and its intended principal separately.
It is **not attached** by this command or by operator bootstrap. Write permission
is limited to `TerminateInstances` on the three exact account/region instance
ARNs, conditional on their recorded ownership tags. Read-only EC2 discovery is
region-scoped and can expose metadata for other resources in that region; these
Describe calls cannot be restricted to the same instance ARNs. The policy does
not grant retagging, direct volume deletion or IAM modification.

These conditions follow the [EC2 authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html).
They are tested with a local policy evaluator, not the live IAM engine. Other
policies attached to the principal may grant broader access; this fragment is
not a permissions boundary. Validate the complete role before authorizing cleanup.
Local journal contents are operator-controlled inputs, not independent ownership
proof; the termination helper must still recheck live resources.

Before any eventual apply, stop traffic, preserve required data and remove exact
owned DNS records. Terminating the VMs deletes their local application, identity
and telemetry data through the verified root-volume deletion policy. The future
operator must deliberately pass `--apply --confirm` with the exact confirmation
printed by preview. No apply command is recommended before live qualification.

The prototype checks account, journal IDs, tags, recorded VPC/subnet placement,
disk/ENI relationships and stable VM state twice. It rejects drift and writes a
private intent before one AWS request.
An existing cleanup journal is never reused for another termination request. A failed/ambiguous response may mean
some instances are already terminating: reconcile the recorded IDs in AWS, never
bypass uncertainty using a new journal. An accepted request is reported with
`termination_verified: false`; wait for actual termination before later stages.

### Observe a termination without retrying it

After an accepted request **or an uncertain response**, use the same cleanup journal:

```sh
python3 tests/external-services/cleanup-instances.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" \
  --journal "$CLEANUP_JOURNAL" --verify
```

This performs only account, instance and volume reads. It requires all three
journaled instances to be observed as `terminated`, with matching ownership tags,
and all three recorded root volumes to be absent. Partial pages, changed ownership
and unexpected IDs are rejected. Remaining disks or missing instance observations
keep `termination_verified` false; they never trigger a retry or another deletion.

Save the nonsecret output with your deployment evidence. EC2 retains terminated
instances in describe responses for only a limited period; [AWS documents that
this is usually less than one hour](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstances.html).
If observations have expired, reconcile manually. An empty instance result alone
is not accepted as proof. Network interfaces, groups, keys, DNS, databases, backups
and VPC resources still require their own cleanup checks.

## Ordered teardown review

Add `--teardown-plan` to the inventory command to include the affected resource
IDs and manual review order:

```sh
python3 tests/external-services/cleanup-inventory.py \
  ".state/deployments/$RUN_PREFIX.json" --state-dir .state --teardown-plan
```

Include `--rds-journal ".state/$RUN_PREFIX-rds.json"` when RDS was provisioned.
This still performs only read operations. The plan orders data preservation and
quiescing, instance termination, optional database deletion, dependency cleanup,
imported public-key removal and finally local-state review. It lists root volumes
that would be deleted with the VMs and keeps final snapshots/automated-backup
retention as explicit decisions. RDS-managed secrets remain service-owned.

**This is not a cleanup executor.** Unresolved interfaces and unverified DNS/IAM,
security-group references, cross-region backups or partial launches require
reconciliation. Even an empty unresolved-interface list does not authorize
deletion. Revalidate live ownership before each manual mutation; retain journals
and keys until cleanup is reconciled.

## Imported SSH key-pair cleanup prototype

After the VM termination verifier reports success, preview removal of the three
**EC2 public-key resources**. This retains local private keys, SSH trust files and
all deployment journals. It does not remove keys from any host's `authorized_keys`.

```sh
python3 tests/external-services/cleanup-keys.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" \
  --termination-journal "$CLEANUP_JOURNAL"
```

The preview requires the immutable key-pair IDs recorded by new launches. Older
journals are refused for this operation. Exact names, ownership tags and remaining
instance references are checked. A live instance using a key prevents deletion.

The explicit apply form additionally accepts `--apply`, a new private `--journal`
directory and the exact `--confirm` string printed by preview. It rechecks the
remaining keys before each deletion and records intent/result separately. A lost
response stops the sequence; never create a new journal to retry uncertain work.
A successful request is reported separately from verified absence in AWS.

### Verify removal or reconcile an interrupted request

Use the key cleanup journal from apply; this command performs no writes:

```sh
python3 tests/external-services/cleanup-keys.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" \
  --journal "$KEY_CLEANUP_JOURNAL" --verify
```

`deletion_verified: true` means all three recorded key IDs are currently absent.
A partial journal is accepted only when its intent/result sequence is valid.
Remaining IDs are reported for review, never retried automatically. This does not
prove removal of a later key pair recreated under the same name, nor does it
remove local keys or establish complete deployment teardown.

### Render exact-resource permissions for administrator review

This reads the source journals and prints policy JSON without contacting AWS or
attaching permissions:

```sh
python3 tests/external-services/cleanup-keys.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" --policy
```

The mutation grants name only the three recorded key-pair names in their ARNs and
require ownership tags and region. AWS defines [key-pair ARNs by name](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html),
not immutable ID. The helper checks/deletes the original IDs; the IAM policy alone
cannot distinguish a same-name replacement carrying the same tags. Read-only discovery is included. The policy grants no
instance termination, database deletion, retagging or IAM administration. It is
not a permissions boundary against other policies already attached to the role.

**Not yet live-qualified:** the generated bootstrap role does not include this
stage's deletion grants. Do not switch to broader credentials to bypass that gap.
Network resources, DNS, RDS, backups and local files still need their own cleanup.

## Print the deletion inventory

In the AWS administrator terminal used for provisioning, print only the non-secret ownership/resource fields from the three launch journals:

```sh
(
  set -euo pipefail
  for role in gateway keycloak observability; do
    jq --arg role "$role" \
      '{Role:$role, AccountId, Region, Prefix, InstanceId, SecurityGroupId, KeyPairName, ApplyStatus}' \
      "$AWS_TEST_REPO/.state/$RUN_PREFIX-$role.json"
  done
)
```

In the matching AWS account and region:

1. Stop the benchmark and retain the selected evidence.
2. If RDS was created, decide on its final snapshot/backup retention, disable deletion protection deliberately, and delete the lab database. Wait for completion before removing its group dependencies.
3. Terminate the three recorded EC2 instances; wait until their network interfaces are released.
4. Remove the lab security-group references, then delete the recorded groups and any lab-owned RDS group.
5. Delete the recorded imported key pairs and any remaining lab-owned EBS volumes. Check retained RDS snapshots/backups and its subnet/parameter groups separately.
6. Check AWS for remaining run-prefix tags and charges. Keep journals until reconciliation is complete, then remove local keys/trust only when no longer needed.

A partial launch can have no instance but still have a security group or key pair. Inspect every journal, including unfinished operations; do not treat an absent field as proof that AWS created nothing. The tables below identify additional retained state.

## Resources created by the current three-VM helper

| Resource | What to check and remove |
| --- | --- |
| Three EC2 instances | Stop traffic, export evidence, then terminate gateway, Keycloak and observability instances. Stopping alone retains storage charges. |
| EBS volumes | Check each root volume's `DeleteOnTermination`; inspect unattached volumes afterward. Delete only volumes belonging to this run. |
| Three security groups | Remove lab inter-group references first, then delete groups after instances/ENIs release them. Do not remove shared VPC groups. |
| Imported EC2 key pairs | The existing helper imports a per-launch key-pair resource; inspect the names/IDs in each role's journal. Delete each owned AWS key-pair entry after instances are gone. |
| Local SSH key and journals | Keep while recovery/evidence is needed, then remove deliberately. Deleting an EC2 key-pair entry does not delete your local private key. |

The older three-VM helper reuses a selected VPC/subnet. The combined
`infrastructure.py` path instead creates a dedicated VPC, internet gateway, route
table, public service subnets, private database subnets and route-table
associations. Preserve its network journal; remove those resources only after
instances, databases and all other network dependencies have been reconciled.
Neither path creates a NAT gateway, load balancer, hosted zone or S3 bucket.

Optional DNS setup creates only the exact run-specific records in an existing
hosted zone. Remove them with the journal-aware DNS helper before retiring the
VMs; never delete the shared zone. The administrator bootstrap creates a group
IAM role and policies that can serve multiple deployments; retiring one deployment
does not authorize deleting that shared group access.

Optional RDS creates its reviewed database, subnet group, parameter group and
security group; RDS manages the master-password secret. Do not delete shared
network infrastructure or independently delete its managed secret. Check for
unexpected residual resources rather than treating this list as proof that no
other resources exist.

## Resources created only when optional RDS apply is selected

| Resource | Manual decision |
| --- | --- |
| RDS instance | Disable deletion protection only after approval. Choose whether to keep a final snapshot/retained automated backups; retained storage continues billing. |
| RDS snapshots, subnet group, parameter group and security group | Inventory/delete separately when no longer needed; snapshots can contain identities, keys and usage history. Do not delete a shared subnet group. |
| RDS-managed Secrets Manager entry | Record the reference and let the RDS lifecycle manage it; this helper does not independently delete it. Review separately any additional secrets created outside the helper. |
| Optional DNS records | Exact run-specific A/TXT records are created only when selected. Use their ownership journal for removal; preserve the shared hosted zone. |
| Elastic IPs, load balancers, NAT gateways, log groups or buckets | These are not created by the current helpers. If added separately, record their IDs and ownership; they can outlive the VMs. |
| Private CA/service credentials | Revoke or retire the entire lab trust domain and OIDC clients, remove lab CA trust from clients, and remove local service keys when no longer needed. |

Use each `.state/RUN_PREFIX-ROLE.json` and the selected RDS recovery journal as the starting inventory. The RDS journal records planned names, completed resource IDs/ARNs and any pending operation; inspect pending operations for ambiguous success before deleting or retrying. verify actual AWS resources and billing after cleanup. Do not paste journal or secret contents into public PR evidence.

## State on retained VMs

Terminating a VM with its root EBS volume removes its local files. If retaining a VM, inventory these separately before reusing it:

| Host | Retained state |
| --- | --- |
| Gateway | Application databases and keys; relay Quadlet/drop-ins and Podman secrets; `enmaas-host-metrics` service/timer; `/etc/enmaas-telemetry-lab`; the service account's private `~/.local/share/enmaas-telemetry-logs`; `enmaas-lab-gateway-log-positions` volume. |
| Keycloak | Realm/client configuration, issuer keys, PostgreSQL volume, service-account secrets and root-private bootstrap inputs. Removing a container alone does not revoke issued tokens or remove its database. |
| Observability | OAuth client/cookie secrets, private TLS material, Perses configuration and Prometheus/Loki/Tempo volumes. Logs and traces can reveal operational metadata even after message redaction. |
| RDS bootstrap | `/root/pricetag-external-db` contains application credentials and the recovery journal. The master credential remains managed by RDS/Secrets Manager; do not confuse removing local files with revoking database roles. |

For a retained gateway, stop the host-metrics timer and relay before removing their configuration. Remove the three `service-logs.conf` drop-ins and the gateway `telemetry.conf` drop-in, reload the user systemd manager, and restart MaaS, PriceTag and the gateway before deleting the private log directory or position volume. Keep application units, secrets and database volumes. Confirm that inference still works before removing retired telemetry credentials. No uninstall command automatically performs these destructive steps.

## Inspect VM security groups after termination

After the termination verifier confirms all three journaled instances terminated
and their root volumes are absent, inspect the remaining group dependencies:

```sh
python3 tests/external-services/cleanup-groups.py \
  --manifest "$MANIFEST" --state-dir "$INFRA_JOURNAL" \
  --termination-journal "$CLEANUP_JOURNAL"
```

Use the same termination journal retained from the VM cleanup step. This command
is read-only. It checks each exact group ID, account, VPC and ownership tags, then
queries remaining interfaces and current-account ingress/egress group references.
Interfaces or references from groups outside the three VM groups block the plan;
they are never adopted or edited. Otherwise the output orders internal-reference
removal before owned-group removal. No deletion command or authorization is emitted.

This complements the pre-termination inventory: it works after the original root
volumes are gone. It still requires AWS to return all three terminated instances;
if those historical records have expired, reconcile manually. It does not inspect
all VPC services, authorize RDS cleanup, resolve cross-account/peered references or
protect a future mutation against changes after inspection. Full network deletion
and its IAM grants remain separate unfinished work.
