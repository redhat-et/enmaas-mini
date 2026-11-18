# AWS infrastructure for the three-node test lab

This guide provisions infrastructure only. For service installation, return to
[the full lab quickstart](README.md). The [one-node AWS helper](../aws-pricetag.md)
remains separate. Keep AWS credentials on your workstation; service installers
on RHEL do not need them.

**Choose the infrastructure path:** [dedicated VPC and three hosts](infrastructure.md)
combines new-network creation and VM launches. The individual VM steps later in
this guide require an already prepared, approved subnet in a dedicated deployment
VPC. The [operator bootstrap](operator-access.md)
covers network/VM grants and an explicitly selected DNS namespace; RDS/cleanup
permissions, public-certificate
setup and complete scripted deletion remain unfinished.
Do not interpret the generated policy fragments as a complete provisioning role.
Use source-restricted access and private-CA trust for this qualification lab.

## Discover account resources before deployment

Run in your own AWS administrator terminal, from the reviewed checkout:

```sh
bash tests/external-services/aws-discover
```

Press Enter for `eu-central-1` (Frankfurt), or enter another deployment region,
then enter the access key ID, secret key and optional
session token at hidden prompts. The script performs only read operations and
writes no credential files. Credentials are held in this process and its AWS CLI
children; they are not exported into the parent terminal. Exiting clears those
process values, not the AWS-issued credential itself. Do not source the script.

It prints the caller account, public zones, registered domains, VPCs with their
`IsDefault` status, candidate subnets
and matching existing VMs, then copyable nonsecret `ACCOUNT`/`REGION` exports.
It does not choose a zone/subnet/VM, create keys, change resources or configure the
deployment. Review the results before the following steps. Empty lists are valid;
unavailable sections produce a partial-result warning and nonzero exit status.

To choose an existing public zone during discovery:

```sh
bash tests/external-services/aws-discover --select-zone
```

Select a numbered zone you are authorized to use, or `0` to skip. The command
prints copyable `ZONE_ID` and `DNS_DOMAIN` exports for the scoped-role and
infrastructure commands. It validates the returned names before displaying them
and excludes private hosted zones. Nothing is saved or changed in AWS.
Selection does not establish delegation or permission to use the domain, and does
not request a public certificate. The current lab still uses a private CA.

No deployment account, domain, zone, IP address or key path is hardcoded. Route 53
Domains uses its service endpoint in `us-east-1`; all regional deployment lookups
use your input. CLI saved profiles/history and configured endpoint overrides are
ignored for this credential-prompt flow. Deployment authentication remains a
separate step after this process exits.

## Preview the deployment settings in one command

No exported variables or AWS credentials are needed for this **local settings
preview**. It prompts for group, account, region, scoped-profile name and SSH-key
path. It does not call AWS, verify permissions/capacity, create a key or save files.

```sh
python3 tests/external-services/deployments.py init --dry-run
```

An empty or invalid group now reports the group error directly. Use 1–8 lowercase
letters/digits, starting with a letter. Do not pass `--group "$DEPLOYMENT_GROUP"`
unless that variable is set. A profile name in the preview does not create or
validate an IAM role.

## Prepare credentials — administrator prerequisite

The intended sequence is:

1. Administrator creates a group-scoped role once, with reviewed trust and permissions.
2. Operator uses an assume-role profile to obtain temporary credentials for that role.
3. Each deployment plan/apply uses that profile and a distinct run ID in its group.

Use [scoped access setup](operator-access.md) for the administrator-run bootstrap
and nonsecret operator profile. The implemented role covers the reviewed network
and VM operations; live permission tests remain pending. Optional DNS grants must be selected explicitly; RDS and compute/network cleanup
permissions are not yet included. Then use [combined infrastructure](infrastructure.md)
for a dedicated VPC and three hosts. Do not attach the policy fragments below as
a complete deployment policy or substitute administrator credentials in operator
commands.

## Save and restore deployment selection

Once the scoped AWS profile is prepared, save the nonsecret settings interactively:

```sh
python3 tests/external-services/deployments.py init &&
python3 tests/external-services/deployments.py list
```

`init` prints the manifest path. Reuse its group/profile for later deployments;
each invocation generates a distinct `em-<group>-<12 random hex digits>` ID.
It records metadata and the SSH key **path**, never credentials or key contents.

For headless use, supply all inputs explicitly; add `--dry-run` to preview without saving:

```sh
python3 tests/external-services/deployments.py init \
  --group "${DEPLOYMENT_GROUP:-}" \
  --account "${ACCOUNT:-}" --region "${REGION:-}" \
  --aws-profile "${AWS_PROFILE:-}" \
  --ssh-key-path "${SSH_KEY:-}"
```

The group tag scopes the planned operator role; the distinct run tag scopes
inventory and cleanup. Tags alone do not restrict existing AWS credentials.
For optional DNS, add `--zone-id` and `--domain` to `init`; see [DNS setup](dns.md).
Do this **before provisioning**: a manifest does not adopt an existing deployment.

The multi-node `aws_lab_deploy` prints a recovery summary after success or a partial
failure. Reprint it with `aws_lab_summary` after sourcing `aws-lab.sh`. It shows the
selected group/account/region, profile and SSH key path, plus recorded VM IDs,
network, disk size, security groups and access rules. `aws_lab_info` adds verified
live IP addresses and connection exports. No key contents or credentials are shown.
The summary is not deletion authorization: use the live cleanup inventory and
review its dependency/retention findings. Automated deletion remains pending.


To restore a saved deployment, substitute its printed path:

```sh
python3 tests/external-services/deployments.py env \
  .state/deployments/REPLACE-WITH-SAVED-ID.json
```

Review and copy the printed shell block into the operator terminal. It clears
previous static AWS credentials, discovery state and selected SSH target before
exporting the saved profile, account, region, run prefix and SSH key path. Then:

```sh
source scripts/aws/session.sh
aws_test_profile "$AWS_PROFILE"
aws_test_discover
```

Discovery must match the saved account. Re-select the intended subnet and source
CIDR before planning infrastructure. This does not reinstall services, restore
provider credentials or verify live resource ownership; use the launch journals
and [cleanup inventory](cleanup.md) for ownership checks. Do not replace the saved
run prefix with the random-prefix example below.

## Provision the three VMs

### Optional policy review before provisioning

The registry can render policy proposals without calling AWS. For an exact,
same-account IAM user, set `TRUSTED_PRINCIPAL_ARN` to the reviewed user ARN:

```sh
umask 077
python3 tests/external-services/deployments.py trust-policy \
  ".state/deployments/$RUN_PREFIX.json" \
  --principal-arn "${TRUSTED_PRINCIPAL_ARN:-}" \
  > ".state/$RUN_PREFIX-trust-policy.json"
```

User trust requires `aws:MultiFactorAuthPresent`. An exact IAM role requires
`--allow-role-principal` and separate review of that role's identity/MFA controls;
the generated role-principal policy makes no MFA guarantee. Account-root,
wildcards, another account and STS session ARNs are rejected. This is a proposal,
not principal-existence validation or an IAM bootstrap operation. See
[AWS principal semantics](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_principal.html)
and [CLI role/MFA setup](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html).

From a saved deployment, generate a **read-only inspection fragment**:

```sh
umask 077
python3 tests/external-services/deployments.py inspection-policy \
  ".state/deployments/$RUN_PREFIX.json" > ".state/$RUN_PREFIX-inspection-policy.json"
```

It contains no credential access, provisioning, deletion or IAM permissions.
EC2 discovery and RDS backup discovery can read regional metadata beyond this
deployment; ownership filters in helpers do not restrict what IAM permits reading. RDS database/parameter/tag
reads target the generated resource names, and DNS reads target the approved zone.
Region conditions apply to regional services, not global Route 53.

An administrator can validate the fragment without attaching it:

```sh
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document "file://.state/$RUN_PREFIX-inspection-policy.json"
```

Review all findings. Validation and live permission tests are still pending; JSON
tests do not establish AWS authorization. This fragment does not include role trust,
STS/profile setup, launch grants or cleanup grants. The administrator must prepare
the full scoped role before using it to provision resources. No long-lived access
keys or role changes are created by this helper.

For the dedicated network, generate the separate **network policy fragment**:

```sh
umask 077
python3 tests/external-services/network-policy.py \
  ".state/deployments/$RUN_PREFIX.json" > ".state/$RUN_PREFIX-network-policy.json"
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document "file://.state/$RUN_PREFIX-network-policy.json"
```

It restricts creation to group/run/purpose request tags and changes to existing
resources with matching ownership tags. It grants tagging only during the listed
creation actions, with no standalone retagging or deletion. It covers the network
command only, not VM launch, RDS, DNS or IAM bootstrap. This is an allow-policy
fragment, not a permissions boundary; attaching broader policies would broaden access.

All runs in a group share this operator's authority. IAM's run pattern constrains
the prefix and suffix length; the helper validates hexadecimal IDs. The helper
also chooses exact routes and attributes: the IAM grants do not enforce that
complete topology. Regional discovery exposes metadata beyond the group. Validate
the fragment and exercise live allowed/denied operations before relying on it.
See [EC2 action/resource/condition support](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html).

The separate `vm-policy.py` fragment accepts explicit reviewed `--image-id`,
`--instance-type` and `--max-volume-gib` values. It restricts launch dependencies
to group-owned subnets/security groups/keys, requires IMDSv2 and encrypted gp3
disks, and grants no instance profile or IAM access. It does not enforce an exact
VM count, total spend, ingress port/CIDR matrix or every relationship between tags;
the launch helper checks the selected deployment and topology.

**Packaging:** these fragments can exceed individual IAM policy size limits.
`policy-pack.py` splits whole statements into documents no larger than 6,144
serialized characters, preserving grants and assigning unique statement IDs.
Its conservative attachment budget is ten documents. This is size checking, not
IAM validation or role creation; review and validate each resulting policy.
Never attach a complete oversized fragment as one inline or managed policy.

```sh
python3 tests/external-services/vm-policy.py \
  ".state/deployments/$RUN_PREFIX.json" \
  --image-id "${RHEL_AMI_ID:-}" \
  --instance-type m7i.2xlarge --max-volume-gib 150 \
  > ".state/$RUN_PREFIX-vm-policy.json" &&
python3 tests/external-services/policy-pack.py \
  ".state/$RUN_PREFIX-network-policy.json" \
  ".state/$RUN_PREFIX-vm-policy.json" > ".state/$RUN_PREFIX-policy-bundle.json"
```

Change the instance type/disk cap to the reviewed plan; repeat `--image-id` or
`--instance-type` for additional approved values. Account policy/attachment
quotas must also be checked during bootstrap; see [IAM quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html).

### Launch

On the AWS administrator workstation, enter the reviewed repository and reuse the [AWS credential/session setup](../aws-pricetag.md). Keep AWS credentials on this workstation. The following block preserves a selected deployment's profile, run prefix and SSH-key path. Without a saved selection, it creates a new run prefix and prompts for credentials. For a new independent deployment, use a fresh terminal or select a newly initialized manifest first.

```console
source scripts/aws/session.sh
REGION="${REGION:-eu-central-1}"
SUBNET="${SUBNET:-}"
CLIENT_CIDR="${CLIENT_CIDR:-}"
RUN_PREFIX="${RUN_PREFIX:-enmaas-lab-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')}"
SSH_KEY="${SSH_KEY:-$HOME/.secure-single-server-tests/ssh/$RUN_PREFIX}"
if [ -n "${AWS_PROFILE:-}" ]; then
  aws_test_profile "$AWS_PROFILE"
else
  aws_test_credentials
fi &&
aws_test_discover &&
aws_test_key &&
ssh-add "$SSH_KEY" &&
source tests/external-services/aws-lab.sh &&
aws_lab_plan
```

Review all plans, then run:

```console
aws_lab_deploy
aws_lab_info
```

Each VM still requires its own typed billable-launch confirmation. `aws_lab_info` waits for running instances and exports and prints each role’s SSH target, public IP and private IP for the following steps. It does not verify service readiness. Defaults: gateway 32 GiB/150 GiB disk, observability 16 GiB/100 GiB, Keycloak 8 GiB/50 GiB. These are test starting points, not measured capacity claims. All use amd64; no GPU or local LLM is required.

**Partial launch:** inspect `.state/$RUN_PREFIX-ROLE.json`. Resume only the failed role with `aws_test_deploy ROLE tests/external-services/ROLE-vm.json --scenario SCENARIO`; gateway uses `remote-gateway`, others use their role. Do not rerun the full batch after a partial failure. The existing helper retains journals and never deletes resources automatically.

Only SSH and browser HTTPS (8443) from the workstation `/32` are initially allowed. Add only the [reviewed peer rules](network.md); do not open raw database, Prometheus, Loki, Tempo or collector ports publicly. See [design and acceptance](design.md) and [manual AWS cleanup](cleanup.md).

## Automated tests

The [mocked AWS and installer suite](../../../tests/aws/README.md) runs without real
credentials and enforces statement/branch coverage for its listed Python modules.
The combined deployment flow is still incomplete; real IAM enforcement and RHEL
installation need separate qualification.

## Dedicated-network preview

The replacement infrastructure flow creates a VPC for each deployment. Its
planner and creation command are implemented with mocked tests; **scoped IAM,
recovery, deletion and live AWS qualification remain unfinished**. Do not use this
as the complete lab deployment flow yet. It never adopts an existing/default VPC.

With a saved group manifest, choose a private CIDR and two or three standard zones
in that manifest's region:

```sh
python3 tests/external-services/vpc-plan.py \
  --manifest .state/deployments/DEPLOYMENT_ID.json \
  --cidr 10.42.0.0/20 --zone eu-central-1a --zone eu-central-1b
```

Replace the example deployment ID, CIDR and zones. The output describes:

- Explicit public routes for service subnets; public IP assignment remains a VM
  launch choice. Security groups determine which client ports are reachable.
- Database subnets in each selected zone with only the implicit VPC-local route.
  No public database endpoint, NAT gateway or shared-network adoption is planned.
- Per-deployment/group ownership tags, placement for all three service nodes and
  cleanup order. Cleanup must reject unknown attachments and retain audit journals.

Add `--live` to the same command for a **read-only preflight** using the manifest's
AWS profile and region. It checks the caller account, selected zone availability,
existing deployment tags and overlapping primary/secondary VPC CIDRs. No resources
or local journals are changed, and credentials are never printed. AWS CLI pagination
remains enabled; an incomplete inventory is rejected.

This checks visible VPCs in that account/region only. It does not qualify launch
permissions, instance capacity, peered/on-premises address ranges or connectivity.
The default command remains offline and does not read AWS credentials.

### Creation and recovery contract

`vpc-create.py` accepts the same manifest, CIDR and zone arguments. Its default is
also an offline plan. Creation additionally requires `--apply`, a new `--journal`
directory under a user-owned `0700` parent, and the exact
`--confirm 'create network DEPLOYMENT_ID'` value. It runs live preflight first.
Use only the scoped deployment profile; the command does not create credentials.

- Every mutation gets a durable intent record before the AWS call and a result
  record afterward. Created resources receive deployment/group tags at creation.
- AWS write retries are disabled. VPC and subnet readiness use bounded read-only
  waits; a timeout stops subsequent operations and retains the journal.
- Existing journal directories are never overwritten or implicitly resumed.
  After any failure, preserve the journal and inspect the deployment's tagged
  resources. A missing result means the AWS outcome is uncertain, not that nothing
  was created. Do not delete the journal and retry with a fresh directory.
- Success reports network resource IDs only. It does not launch VMs, grant ingress,
  create credentials or prove live connectivity. Automated recovery and teardown
  must be completed before this becomes the normal infrastructure entry point.

References: [AWS VPC creation](https://docs.aws.amazon.com/vpc/latest/userguide/getting-started-with-amazon-vpc-using-the-aws-cli.html),
[AWS retry settings](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-retries.html),
[VPC readiness](https://docs.aws.amazon.com/cli/latest/reference/ec2/wait/vpc-available.html).

Standard RDS subnet groups need subnets in at least two zones, including for
Single-AZ databases. This network layout does not make the application highly
available. See [RDS VPC requirements](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html).
Before deleting a VPC, its service-managed interfaces and custom dependencies
must be removed; the future executor must verify ownership before each action.
See [AWS VPC deletion requirements](https://docs.aws.amazon.com/vpc/latest/userguide/delete-vpc.html).
