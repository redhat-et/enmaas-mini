# AWS and gateway installer tests

These tests use fake AWS clients/CLI responses and synthetic credentials. They do
not provision infrastructure or need an AWS account. The one-node helper remains
covered alongside the new group/deployment, DNS, RDS and inventory helpers.

## Run the mocked coverage suite

From the repository root:

```sh
python3 -m venv .venv-aws-tests &&
.venv-aws-tests/bin/python -m pip install -r tests/aws/requirements.txt &&
.venv-aws-tests/bin/python tests/aws/run-mocked.py
```

Open `.state/coverage/html/index.html`; machine-readable results are in
`.state/coverage/aws.json`. Reports measure branches and statements, including
Python CLI subprocesses. Shell-script branches are not measured by coverage.py;
Bash/Zsh/PTY behavior is exercised by separate contract tests.

The runner replaces AWS environment/configuration with dummy values and a temporary
home. A rejecting `aws` shim catches unexpected unmocked CLI calls. In-process
socket connection attempts fail. Tests that exercise credential prompting install
their own fake AWS executable. No real AWS credentials are required or loaded by
the runner. This is test isolation, not a sandbox against malicious test code.

## CI status and coverage target

`Mocked AWS and installer coverage` runs this suite on pull requests and main when
related code changes. It uses read-only repository permissions, no AWS secrets or
OIDC token grant, and publishes only coverage JSON/HTML. Test failures fail the job.
The workflow must be pushed before GitHub runs it.

**100% statement and branch coverage is enforced separately for each Python
module enumerated in `coverage.ini` and `run-mocked.py`.** No production branches are excluded to reach that result.
This does not measure every script in the repository or prove deployment safety:
add new orchestration modules to `coverage.ini`, the suite and its per-file gate.
A successful mocked job is not complete installation acceptance. On macOS, the
Bash-4 startup contract is skipped if only system Bash 3 is available; Ubuntu CI
supplies a newer Bash.

Coverage includes:

- `tests/external-services/zone-select.py`: public-only metadata validation, bounded
  terminal selection and shell-safe nonsecret exports; fake-AWS PTY tests cover its
  discovery integration without accessing real credentials.

- `tests/external-services/import-population.py`: credential-free profile validation,
  exact issuer UUID mapping, export-bound receipts and refusal to replay uncertain writes.

- `scripts/aws/environment.py`: named-profile credential isolation, unchanged
  environment-only authentication without a profile, disabled endpoint overrides
  and automatic prompting/retries. No real credentials are loaded by these tests.
- `tests/external-services/services.py`: source-pinned coordinated installation,
  protected transfers, durable stage receipts and offline access summaries.
- `tests/external-services/personas.py`: immutable issuer identities, authenticated
  role/team setup, budget readback and refusal to replay uncertain privilege writes.
- `tests/external-services/population-key.py`: selected key export, private-file/hash
  checks, finite expiry and current identity/key state.
- `scripts/aws/rhel-vm`: planning, ownership, launch and retry contracts.
- `tests/external-services/deployments.py`: registry, validation and policy fragments.
- DNS, peer-network, RDS and cleanup-inventory helpers.
- `tests/external-services/populate-keycloak.py`: loopback TLS administration, token
  refresh, fixed-origin UUID validation and explicit issuer-host command.
- `tests/external-services/user-population.py`: private durable records, concurrent-run
  exclusion, uncertain-create refusal and resume using the recorded issuer UUID.
- `tests/external-services/keycloak-password.py`: explicit selected-identity password
  retrieval, completed population-record checks, private-file validation and service-credential exclusion.
- `tests/external-services/transfer-client.py`: SSH trust options, bounded secret transfer,
  failed-source refusal, error redaction, credential-free population validation and atomic destination writes.
- `tests/external-services/infrastructure-info.py`: read-only host summary, journal
  ownership and running-state checks, compatible peer-network journals and safe output.
- `tests/external-services/infrastructure.py`: combined network/three-host orchestration,
  offline preview, restricted ingress, pre-launch validation and retained failure journals.
- `tests/external-services/vpc-plan.py`: topology and read-only account/zone/CIDR preflight.
- `tests/external-services/vpc-create.py`: creation sequence with durable intent/result
  journals, creation-time tags, private database routes and stop-on-uncertainty behavior.
  The command defaults to an offline plan; explicit apply, disabled write retries
  and bounded read-only readiness waits are tested. Recovery/cleanup integration
  and live AWS qualification remain unfinished.
- `tests/external-services/vpc-inspect.py`: offline journal sequencing, private-file
  checks, confirmed/uncertain outcome reporting and redacted failure diagnostics.
- `tests/external-services/network-policy.py`: group/account/region restrictions,
  required creation tags, existing-resource tags and creation-only tagging grants.
  The test evaluator covers this fragment's condition vocabulary, not AWS IAM.
- `tests/external-services/vm-policy.py`: approved AMIs/types, IMDSv2, encrypted gp3
  disks, group-owned launch dependencies and creation-only tagging.
- `tests/external-services/policy-pack.py`: bounded managed-policy documents,
  unchanged grants, unique statement IDs and attachment-budget refusal.
- `tests/external-services/operator-profile.py`: MFA/source-role settings, private
  config handling, preservation of existing bytes, overwrite refusal and atomic writes.
- `tests/external-services/operator-bootstrap.py`: offline preview, explicit apply,
  profile isolation, account/name/quota/Access Analyzer gates and journaled IAM writes.
- `scripts/pricetag/install.py`: arguments, mock/real orchestration, remote-service
  configuration forwarding, retained-state refusal and stage failures. The suite
  replaces host/service boundaries; it does not execute RHEL package installation.

## Extend coverage with the workflow

1. Include remaining IAM lifecycle, network recovery/deletion as they are implemented. The existing service
   coordinator and VM-termination prototype and read-only result verification are covered; full teardown is unfinished.
2. Test partial completion, retries and foreign-resource refusal for each addition.
3. Keep shell/interactive contracts explicit; Python coverage does not measure them.
4. Run runtime qualification separately for Keycloak, observability and gateway
   services, including synthetic-account provisioning and mock-to-real replacement.

## Tests that mocks cannot replace

Real AWS acceptance must verify IAM deny behavior, resource/tag-condition support,
DNS propagation, service dependencies and cleanup. Separate-host RHEL acceptance
must verify actual installation, SELinux/firewalld, certificates, reboot recovery,
private communication and dashboards. Local Podman runtime tests cover selected
identity and telemetry boundaries but do not establish AWS/RHEL readiness.
