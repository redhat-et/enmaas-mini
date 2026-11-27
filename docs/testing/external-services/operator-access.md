# Scoped AWS operator access

Run this setup **in the AWS administrator's workstation terminal**. Application
hosts receive no AWS credentials. The resulting role manages network and VM
resources for one deployment group, account and region across multiple runs.

**Qualification in progress:** the command and failure paths have mocked tests.
Actual role assumption/IAM allow/deny tests, role retirement, RDS/cleanup grants and the combined
four-command deployment workflow remain pending. This role is not yet sufficient
for the full external-services deployment.

## 1. Preview the role and grants

Use an existing administrator AWS CLI profile; do not put credentials in command
arguments. Select a short group, the intended operator's exact IAM principal ARN,
and a reviewed regional RHEL AMI. Set `DEPLOYMENT_GROUP`, `ACCOUNT`,
`TRUSTED_PRINCIPAL_ARN`, `RHEL_AMI_ID` and `ADMIN_PROFILE` first. No deployment
manifest is needed yet. Missing values are rejected by the helper without closing
your terminal.

```sh
BOOTSTRAP_ARGS=(
  --group "${DEPLOYMENT_GROUP:-}"
  --account "${ACCOUNT:-}"
  --region "${REGION:-eu-central-1}"
  --principal-arn "${TRUSTED_PRINCIPAL_ARN:-}"
  --image-id "${RHEL_AMI_ID:-}"
  --instance-type m7i.2xlarge --instance-type m7i.xlarge --instance-type m7i.large
  --max-volume-gib 150
)
python3 tests/external-services/operator-bootstrap.py "${BOOTSTRAP_ARGS[@]}"
```

This prints the trust and packed permission documents without calling AWS or
writing files. Review all grants. Repeat `--image-id`/`--instance-type` to approve
additional values. The example approves the three sizes selected by the current
infrastructure command: gateway `m7i.2xlarge`, observability `m7i.xlarge` and
Keycloak `m7i.large`.

An IAM user must assume the role with MFA. For an exact IAM role principal, add
`--allow-role-principal` only after reviewing how that source role authenticates
its users; this path does not assert MFA. Root/wildcard/STS-session principals are
rejected. Group credentials can manage all runs in the group, not just one run.

### Optional DNS namespace

Before applying, add both values to the reviewed arguments if this group will
use the existing DNS helper:

```sh
BOOTSTRAP_ARGS+=(--dns-zone-id "${ZONE_ID:-}" --dns-domain "${DNS_DOMAIN:-}")
python3 tests/external-services/operator-bootstrap.py "${BOOTSTRAP_ARGS[@]}"
```

Choose an approved public zone and domain/suffix inside it; no domain is hardcoded.
The role can create/delete A or TXT records only for `gateway`, `keycloak`,
`observability` and `_enmaas-owner` beneath `em-GROUP-<12 characters>.DOMAIN`.
The helper generates 12 hexadecimal characters; IAM's `?` pattern limits length,
not the hexadecimal alphabet. This is a group namespace, not per-run isolation.

The grant excludes UPSERT, hosted-zone creation/deletion, other groups/domains and
ACME challenge names. Read access includes **all record metadata in the selected
zone**; it cannot be narrowed to the four names by these list permissions.
The DNS helper additionally verifies the public-zone relationship, refuses
collisions and requires its exact immutable records for deletion. IAM does not
validate ownership nonce values or record contents. Existing roles are still
refused by bootstrap; these options do not silently update a deployed role.

Route 53 uses [record-name/type/action conditions](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/specifying-conditions-route53.html),
not deployment tags, for this scope. Policy generation and simulated namespace
tests are not live IAM qualification. This grant does not create DNS records,
or issue certificates. The [infrastructure handoff](infrastructure.md#optional-deployment-dns-handoff)
uses this optional grant after hosts are running.

## 2. Explicitly create the role

In the same Bash/zsh terminal, use the reviewed arguments:

```sh
umask 077
mkdir -p .state/operator-access &&
chmod 700 .state/operator-access &&
python3 tests/external-services/operator-bootstrap.py "${BOOTSTRAP_ARGS[@]}" \
  --apply --admin-profile "${ADMIN_PROFILE:-}" \
  --journal ".state/operator-access/$DEPLOYMENT_GROUP" \
  --confirm "create operator $DEPLOYMENT_GROUP $ACCOUNT"
```

The journal's parent must be a user-owned `0700` directory; the journal directory must not exist.
The administrator profile is used explicitly; unrelated static credentials in
the shell are not forwarded. The named profile must resolve its own credentials
(for example through its configured SSO or source profile); a profile relying on
`credential_source=Environment` is not supported by this isolated path. No AWS
configuration or credential files are written.

Before any IAM writes, the command checks:

- Non-root caller identity and the selected AWS account.
- Absence of the target role and policy names; existing objects are never adopted.
- Available role, managed-policy and attachment quotas.
- Access Analyzer validation of the trust and every permission document. Any
  finding or incomplete response stops creation for review.

Writes create the role under `/enmaas/`, create group-tagged managed policies and
attach them. The role permits sessions up to one hour. The output contains role
and policy ARNs plus the journal path; it creates no users or access keys.

## 3. Configure the scoped operator profile

Use the existing source profile for the principal approved in the role's trust
policy. The example uses an IAM user with MFA. Set `SOURCE_PROFILE` and
`MFA_SERIAL` (device ARN or hardware serial); neither is a credential value.

```sh
PROFILE_ARGS=(
  --account "$ACCOUNT" --region "${REGION:-eu-central-1}"
  --group "$DEPLOYMENT_GROUP"
  --source-profile "${SOURCE_PROFILE:-}"
  --mfa-serial "${MFA_SERIAL:-}"
)
python3 tests/external-services/operator-profile.py "${PROFILE_ARGS[@]}"
```

After reviewing the settings, append the profile to your existing private AWS
config file. The file's parent directory must be user-owned mode `0700`; an
existing config must be a user-owned private regular file. Do not edit the AWS
config in another tool while this command runs.

```sh
python3 tests/external-services/operator-profile.py "${PROFILE_ARGS[@]}" \
  --apply --config-file "${AWS_CONFIG_FILE:-$HOME/.aws/config}"
```

This creates profile `enmaas-$DEPLOYMENT_GROUP`, referring to the scoped role and
source profile. It preserves existing config bytes, refuses an existing target
profile and writes no keys or tokens. Use `--profile NAME` for a different target
name. For a source role explicitly approved with `--allow-role-principal` during
bootstrap, replace `--mfa-serial ...` with `--role-source`; authentication/MFA must
then be enforced at the source role. Do not use that option to bypass user MFA.

Authenticate the source profile through your normal process, then verify the
assumed identity yourself:

```sh
export AWS_PROFILE="enmaas-$DEPLOYMENT_GROUP"
AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true AWS_CLI_AUTO_PROMPT=off \
  aws --profile "$AWS_PROFILE" --region "${REGION:-eu-central-1}" \
  sts get-caller-identity
```

The result must show your selected account and assumed role
`em-$DEPLOYMENT_GROUP-operator`. Stop if it shows the source/admin identity.
The AWS CLI prompts for MFA when required and caches temporary role credentials
under `~/.aws/cli/cache`; it can refresh them and prompt again after expiry.
That cache is separate from the nonsecret profile settings and deployment
journals. See [AWS role profiles and MFA](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html).
This identity check does not qualify the role's permissions.

## 4. Preserve recovery evidence and qualify access

Each write has a durable intent record before AWS and a result record afterward.
A timeout, mismatched resource identity or failure stops the sequence. Preserve
partial resources and the journal for review; do not retry by removing the journal
or reuse an existing name. Automated IAM recovery/retirement is not implemented.

A created role is **not** a qualified role. Before using it for infrastructure,
use the scoped profile above to test permitted actions and
rejection of cross-group/account/region operations, standalone retagging, IAM
administration and secret access. Live assumption and authorization tests remain part
of the unfinished combined workflow. Do not substitute administrator credentials
for missing deployment permissions.

The grants require approved AMIs/types, IMDSv2, encrypted gp3 disks and ownership
tags. They do not cap total spend/VM count or enforce the complete ingress matrix;
the deployment helper validates exact resources and network rules. No compute/network cleanup, RDS, instance-profile or IAM-administration grant is
included. DNS record grants are present only when the optional zone/domain was
explicitly selected.

References: [role creation](https://docs.aws.amazon.com/cli/latest/reference/iam/create-role.html),
[Access Analyzer validation](https://docs.aws.amazon.com/cli/latest/reference/accessanalyzer/validate-policy.html),
[IAM account quotas](https://docs.aws.amazon.com/cli/latest/reference/iam/get-account-summary.html).

## AWS client boundary

The Python deployment clients select the reviewed AWS profile and region, ignore
custom endpoint settings, disable automatic CLI prompting and use one attempt per
AWS call. Failures retain journals instead of automatically repeating a write.
This applies to network/VM, DNS, RDS and IAM-bootstrap calls. The existing one-node
helper still supports environment-only credentials when no profile is selected.

Endpoint isolation uses the documented
[`AWS_IGNORE_CONFIGURED_ENDPOINT_URLS` setting](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html#envvars-list-AWS_IGNORE_CONFIGURED_ENDPOINT_URLS).
These scripts target real AWS, not a local AWS emulator. The mocked suite replaces
the subprocess boundary and forbids network access; it never needs emulator or
real credentials. This local check does not replace live IAM allow/deny tests.

The shared VM AWS client bounds each CLI call to 120 seconds. A timeout stops
execution with a sanitized error and no automatic retry. Terminating the CLI does
not cancel a request already accepted by AWS: preserve the recovery journal and
inspect live resources before deciding how to recover. This timeout is not a
bound on the whole infrastructure installation.
