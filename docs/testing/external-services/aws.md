# AWS infrastructure for the three-node test lab

This guide provisions infrastructure only. For service installation, return to
[the full lab quickstart](README.md). The [one-node AWS helper](../aws-pricetag.md)
remains separate. Keep AWS credentials on your workstation; service installers
on RHEL do not need them.

**Current limitation:** this helper requires an already prepared, approved subnet
in a dedicated deployment VPC. Automatic VPC creation, group-scoped IAM bootstrap,
public-certificate setup and complete scripted deletion are not implemented.
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

No deployment account, domain, zone, IP address or key path is hardcoded. Route 53
Domains uses its service endpoint in `us-east-1`; all regional deployment lookups
use your input. CLI saved profiles/history and configured endpoint overrides are
ignored for this credential-prompt flow. Deployment authentication remains a
separate step after this process exits.

## Save and restore deployment selection

Optional: use a named AWS profile and a private local manifest instead of keeping
selection only in the current terminal. This records metadata and the SSH key
**path**, never credentials or key contents. IAM/profile creation is not automated.

```sh
python3 tests/external-services/deployments.py init \
  --group "$DEPLOYMENT_GROUP" --account "$ACCOUNT" --region "$REGION" \
  --aws-profile "$AWS_PROFILE" --ssh-key-path "$SSH_KEY"
python3 tests/external-services/deployments.py list
```

Choose `DEPLOYMENT_GROUP` once: 1–8 lowercase letters/digits, starting with a
letter. Reuse that group and AWS profile for subsequent deployments. `init`
prints a private manifest path and assigns `em-<group>-<12 random hex digits>`.
The group tag scopes the planned operator role; the distinct run tag scopes
inventory and cleanup. IAM/profile creation is still a separate administrator
operation; these labels alone do not restrict existing AWS credentials.
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
