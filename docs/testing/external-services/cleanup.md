# Manual AWS cleanup

Run cleanup from the AWS administrator workstation or console. The VM/RDS helpers do not delete resources. The separate [DNS helper](dns.md) can delete exact journaled DNS records after explicit confirmation. Keep the launch journals and export sanitized benchmark evidence before deleting anything. Match the account, region, run prefix and resource IDs; names alone are insufficient ownership evidence.

## Read-only inventory for registry-created deployments

For a deployment created with the new registry-generated run prefix, the following
command compares its three VM journals with live EC2 resources. Run it in your
AWS operator terminal using the existing profile recorded in the manifest:

```sh
python3 tests/external-services/cleanup-inventory.py \
  ".state/deployments/$RUN_PREFIX.json" --state-dir .state
```

It requires the recorded account, region, role, instance/group/key identifiers,
ownership tags and root-volume relationships to agree. Missing or extra tagged
resources stop the inventory for reconciliation. It refuses unexpected disks,
foreign attachments and changed deletion flags. Only resource metadata is printed.

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

The three-VM helper reuses the selected VPC/subnet and creates no NAT gateway, load balancer, DNS zone, IAM role, RDS or S3 bucket. The separate optional RDS helper creates only its reviewed database, subnet group, parameter group and security group; RDS manages the master-password secret. Do not delete shared network infrastructure. Check the console for unexpected residual resources rather than treating this list as proof that no other resources exist.

## Resources created only when optional RDS apply is selected

| Resource | Manual decision |
| --- | --- |
| RDS instance | Disable deletion protection only after approval. Choose whether to keep a final snapshot/retained automated backups; retained storage continues billing. |
| RDS snapshots, subnet group, parameter group and security group | Inventory/delete separately when no longer needed; snapshots can contain identities, keys and usage history. Do not delete a shared subnet group. |
| Secrets Manager entries | Delete only lab-owned bootstrap/application secrets under an explicit recovery-window policy; credentials must also be revoked. |
| Elastic IP, DNS records, load balancers, NAT, log groups or buckets | None are created by this draft. If added manually/later, track their IDs/tags and remove them explicitly; these can outlive VMs. |
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

## Local test fixtures

Use the matching `ENMAAS_LAB_INSTANCE` with `tests/external-services/local.py down`. It removes only the labelled fixture pod and secrets; named data volumes and private `.state` files remain. The local Podman runbook lists the default fixture's volume names. For a named fixture, inspect the matching `enmaas-INSTANCE-*` resources rather than running a global prune.

The isolated RDS-role and file-log runtime tests remove their own randomly named containers, volumes and secrets on normal completion or handled failure. After interruption, inspect resources labelled for the test before removing them. Keep private evidence until review is complete; never upload the whole `.state` directory.
