# Optional RDS test database

**AWS runtime qualification is pending.** `tests/external-services/rds.py` supports offline planning, read-only AWS discovery, explicitly confirmed creation and read-only verification. The mutation and recovery paths have simulated-AWS tests; no actual RDS deployment has been performed. Do not treat its output as deployment evidence or submit its nested `Instance` object directly: the dedicated security-group ID is resolved during the future apply step.

The planned deployment is one small PostgreSQL RDS instance for the gateway's MaaS and PriceTag databases. Keycloak retains its separately installed database in this first lab. Separate database roles and credentials are required for each gateway service; the RDS administrator credential must not become an application credential.

## Security and lifecycle choices

- **Private endpoint:** no public address; TCP 5432 only from the gateway security group in the same VPC. No workstation CIDR or observability-host access.
- **Encryption:** encrypted gp3 storage, `rds.force_ssl=1`, client `sslmode=verify-full` using the RDS endpoint hostname and trusted RDS CA bundle.
- **Bootstrap credential:** RDS manages the master password in Secrets Manager. The helper must not print or journal its value. AWS credentials stay on the administrator workstation.
- **Bounded test sizing:** `db.t4g.small` or `db.t4g.medium`, 20 GiB initial storage with 40 GiB autoscaling ceiling, one day of automated backups. This is not measured production sizing or an HA deployment.
- **Deletion:** deletion protection enabled. Removing protection, choosing a final snapshot and deleting resources are explicit administrator operations. Retained snapshots and Secrets Manager entries need separate inventory.

RDS requires subnets in at least two Availability Zones for this regional deployment, including Single-AZ instances. The live planner must check that subnets, gateway and security group belong to the explicit account/VPC and that the chosen PostgreSQL version/class is orderable. See [RDS VPC requirements](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html).

## Read-only AWS plan

Run on the AWS administrator workstation after deploying the gateway. Reuse its launch journal; enter two existing subnet IDs and an explicit PostgreSQL minor version. The helper validates account, gateway/security-group ownership, VPC/DNS, subnet availability and whether the selected version/class supports encrypted gp3 storage.

```sh
python3 tests/external-services/rds.py \
  --gateway-journal ".state/$RUN_PREFIX-gateway.json" \
  --account-id "$ACCOUNT_ID" --region "$REGION" --prefix "$RUN_PREFIX" \
  --subnet-id "$RDS_SUBNET_A" --subnet-id "$RDS_SUBNET_B" \
  --engine-version "$RDS_ENGINE_VERSION" --instance-class db.t4g.small
```

`ACCOUNT_ID` is the explicit target AWS account; it must match STS and the journal. An optional `--profile` selects the workstation AWS profile. The helper has a read-only operation allowlist and never retrieves secret values. Unit tests use simulated AWS responses; real-account validation remains pending.

## Create and verify

After reviewing the read-only plan, repeat it with `--apply` and a new journal path:

```sh
python3 tests/external-services/rds.py \
  --gateway-journal ".state/$RUN_PREFIX-gateway.json" \
  --account-id "$ACCOUNT_ID" --region "$REGION" --prefix "$RUN_PREFIX" \
  --subnet-id "$RDS_SUBNET_A" --subnet-id "$RDS_SUBNET_B" \
  --engine-version "$RDS_ENGINE_VERSION" --instance-class db.t4g.small \
  --apply --journal ".state/$RUN_PREFIX-rds.json"
```

This is billable. The command requires the exact confirmation printed in your terminal, then revalidates the plan. It creates a dedicated security group, subnet group, TLS parameter group and RDS instance. Each operation is journaled before it starts and after its ID is known. The instance uses only the newly created gateway-only security group. No master-password value is retrieved.

An existing journal is never overwritten. **On failure, do not remove the journal and rerun blindly.** Inspect its `Pending` operation and the tagged AWS resources; a timed-out request may have succeeded. The helper does not adopt, resume or delete resources automatically. Reconcile and clean up explicitly before starting a new run.

After RDS provisioning, run:

```sh
python3 tests/external-services/rds.py \
  --verify ".state/$RUN_PREFIX-rds.json" \
  --account-id "$ACCOUNT_ID" --region "$REGION"
```

Verification checks network isolation, storage encryption/bounds, backups, deletion protection, managed credential and TLS parameter state. `Ready: true` means AWS configuration is available; it does not prove application roles, network connectivity, certificate validation or migrations. Bootstrap and end-to-end integration remain the next acceptance step.

## Ownership verification

Verification compares the database, subnet-group and parameter-group ARNs with
the creation journal, checks their ownership tags and the security-group tags,
and requires the managed master-secret ARN to match its recorded reference.
New journals also record the database's immutable `DbiResourceId`; verification
rejects a replacement database even when its name/ARN matches. A missing resource
ID in an older journal requires manual reconciliation, not automatic adoption of
the current database. Preserve the original creation evidence.
It never reads the secret value. Missing or changed references stop verification;
do not repair a mismatch by copying live IDs into the journal without investigating
why ownership changed. Incomplete creation still requires reconciliation.

These checks support a future cleanup plan but do not delete resources or qualify
AWS IAM permissions. Snapshot/backup retention and managed-secret lifecycle remain
separate cleanup decisions.

## Bootstrap application credentials

Run this once for a **fresh** RDS instance. The helper refuses existing `maas` or `metering` roles/databases and an existing output directory. It creates two restricted login roles, separate owned databases and private installer inputs. It verifies TLS and rejects elevated roles or cross-database `CONNECT` permission. It does not rotate or adopt existing credentials.

On the gateway, install the PostgreSQL client and obtain the public RDS trust bundle:

```sh
sudo dnf install -y postgresql
curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  -o "$HOME/rds-ca.pem"
```

From the **AWS administrator workstation**, set `RDS_HOST` and `RDS_SECRET_ARN` from the verified RDS configuration. `REMOTE_REPO` is the absolute reviewed checkout path on the gateway. The SSH identity and host must be configured as in the lab setup. This streams only the database administrator secret over SSH; AWS credentials remain on the workstation. Disable shell tracing and use a terminal without session-input recording.

```sh
set +x
set -o pipefail
REMOTE_REPO=/home/ec2-user/enmaas-mini
printf -v BOOTSTRAP_COMMAND '%q ' sudo python3 \
  "$REMOTE_REPO/tests/external-services/rds-bootstrap.py" \
  --host "$RDS_HOST" --ca-file /home/ec2-user/rds-ca.pem
aws secretsmanager get-secret-value --region "$REGION" \
  --secret-id "$RDS_SECRET_ARN" --query SecretString --output text | \
  ssh "${SSH_OPTS[@]}" "$RHEL_HOST" "$BOOTSTRAP_COMMAND"
```

Use these options with the gateway installer **on the gateway**:

```sh
--maas-database-url-file /root/pricetag-external-db/maas.url \
--metering-database-url-file /root/pricetag-external-db/metering.url \
--external-database-ca-file /root/pricetag-external-db/ca.crt
```

See the [external-database installation instructions](../../quickstarts/remote-gateway-pricetag/external-database.md) for the complete command. The output directory is root-only; application passwords are not printed. Temporary administrator password files are removed on normal completion or handled failure. The private output and `bootstrap.json` remain on failure: inspect the recorded stage and database state before recovery. Do not delete the output and blindly rerun—database creation is not one atomic transaction.

Local validation:

```sh
python3 -m unittest discover -s tests/external-services -p test_rds_bootstrap.py
python3 tests/external-services/rds-bootstrap-runtime.py
```

The runtime test uses a new network-disabled PostgreSQL container. It verifies SCRAM login over `verify-full` TLS, rejects an incorrect certificate hostname and an untrusted CA, and checks own-database migrations, cross-database denial and forbidden role creation. It removes only its temporary container and secret. This qualifies the local PostgreSQL client path; the RDS administrator's privileges, AWS CA bundle, enforced rejection of non-TLS connections and RDS connectivity still require real-infrastructure acceptance.

## Remaining implementation and acceptance

1. Qualify the implemented gateway journal/account/resource checks against the supplied AWS infrastructure.
2. Qualify discovery of subnet, engine-version and instance-class choices; review the printed non-secret plan.
3. Qualify confirmed creation and the recovery journal with a real AWS run. Simulated tests cover failure at every operation boundary and refusal to overwrite an existing journal.
4. Qualify the implemented read-only verifier against the resulting endpoint, encryption, ingress, parameter group, backup policy and managed-secret configuration.
5. Bootstrap isolated application roles without transferring AWS credentials to a VM, then consume the existing external-database installer inputs.
6. Qualify hostname/certificate verification, denied non-TLS access, migrations, accounting, reconnection and cleanup on supplied infrastructure.

Sources: [RDS create-instance options](https://docs.aws.amazon.com/cli/latest/reference/rds/create-db-instance.html), [PostgreSQL TLS and hostname verification](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html). See the [cleanup inventory](cleanup.md).
