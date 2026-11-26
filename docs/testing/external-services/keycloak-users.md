# Synthetic issuer-user population

Use this testing command on the dedicated Keycloak host after issuer installation.
It creates synthetic accounts through the **loopback HTTPS** administration API.
It does not expose administration publicly, configure PriceTag users or issue
inference keys.

**Qualification:** the transport and durable state machine passed against the
pinned Keycloak in isolated local containers using 10,000 users and a repeat run. This is historical issuer-only evidence, separate from the smaller combined-stack acceptance tests.
Selected-password retrieval and credential-free export passed; all test users and
resources were removed. RHEL bootstrap login, PostgreSQL-backed issuer scale,
cross-host PriceTag mapping and persistent-population cleanup remain unqualified. Start with a small disposable population.

## Coordinated installation

The [service coordinator](three-host-install.md) runs population creation,
credential-free export, verified SSH transfer and PriceTag identity import after
service installation. `population_count` selects 1–10,000 accounts. Do not also run
the fresh manual setup below against those same records. Passwords remain on the
issuer. The coordinator then verifies ordinary user roles, sets a $5 monthly
USD override and saves a finite 24-hour inference key for each imported identity.
Bulk users remain ordinary users. A separate coordinator step configures the
three dashboard personas: Super admin, Alice as manager and Bob as Alice's team
member. The issuer's immutable UUIDs are preserved for both groups.

## Create or verify a population

From the installed checkout on the issuer host:

```sh
sudo python3 tests/external-services/populate-keycloak.py \
  --state /root/enmaas-test-users --count 20 --yes
```

The command reads the generated `lab-bootstrap` credential from the root-private
`/root/enmaas-keycloak-inputs/credentials.json`. It verifies the local server with
`/root/enmaas-tls/ca.pem`; it does not disable certificate checking. Credentials
and access tokens stay out of command arguments and output. This is a test-host
bootstrap credential, unrelated to AWS credentials.

The transport refreshes the short-lived access token before expiry. A failed or
redirected API request stops processing; user-creation POSTs are never retried
automatically. If the temporary bootstrap administrator has been removed or its
password changed, authentication fails before allocating new population state.
Do not recreate the administrator or reset its password to bypass that failure.

The coordinator uses `/root/enmaas-tls/ca.pem`. If you followed the manual
[issuer installation](keycloak-rhel.md), add
`--ca /root/enmaas-issuer-tls/ca.pem` to each `populate-keycloak.py` command
instead. Use the installed issuer's existing CA; do not generate a new one.

## State and repeat runs

| Record state | Meaning | Repeat-run behavior |
| --- | --- | --- |
| `prepared` | Generated password is safely recorded; creation has not started | Create the user and record the UUID returned by Keycloak |
| `creating` | A create was attempted but its returned UUID is not durably recorded | Stop for reconciliation; do not infer ownership from a matching username/email |
| `created` | The issuer UUID is recorded | Verify that same UUID, username and enabled state; do not recreate the account |

Keep the same `--state` and `--count` on repeat runs. The private directory holds
`population.json` and one numbered record per user, including the initial password.
Do not publish or copy it wholesale. Concurrent runs are refused. A successful
summary reports the verified issuer count and `pricetag_provisioned: false`.

There is no automated uncertain-create reconciliation or deletion command yet.
Preserve the records after failure; starting a new directory creates a different
population and does not repair the first one.

## Export identities without passwords

After creation, rerun with the same state/count and a new export path on the issuer:

```sh
sudo python3 tests/external-services/populate-keycloak.py \
  --state /root/enmaas-test-users --count 20 --yes \
  --export /root/enmaas-test-users/profiles.json
```

The export contains only issuer UUIDs, synthetic email addresses and display names.
It contains no passwords, tokens, client secrets, roles or budgets. Existing output
files are not overwritten. Keep the export private even though it has no credentials.

## Import identities on the gateway

The importer passed against local PriceTag with two synthetic UUIDs: anonymous
reads were denied, imported IDs were read back, repeat imports made no new writes,
and both test identities were deactivated. The complete issuer-to-gateway handoff
and RHEL installation remain unqualified.

On the operator workstation, use the SSH key and independently verified host-key
file from issuer setup. `KEYCLOAK_RHEL_HOST` and `RHEL_HOST` must be explicit
`user@IPv4` values for the issuer and gateway respectively:

```sh
python3 tests/external-services/transfer-client.py \
  --client population --issuer "$KEYCLOAK_RHEL_HOST" --destination "$RHEL_HOST" \
  --ssh-key "$SSH_KEY" --known-hosts "$KNOWN_HOSTS"
```

The helper copies only `/root/enmaas-test-users/profiles.json` to
`/root/enmaas-inputs/population-profiles.json`. It validates the credential-free
schema before sending data, checks source permissions, and writes an exclusive
mode-`0600` destination. The destination directory must already be root-owned
mode `0700`. Existing files are refused; a failed transfer does not authorize
overwriting them. The issuer's credential records are never transferred.

From the installed checkout on the gateway:

```sh
sudo python3 tests/external-services/import-population.py \
  --profiles /root/enmaas-inputs/population-profiles.json \
  --state /root/enmaas-population-import --yes
```

The helper uses [PriceTag's private partner-user API](../../quickstarts/remote-gateway-pricetag/external-auth.md#register-an-issuer-identity)
through loopback, preserving the exact issuer UUID. It does not write SQL or match
users by email. The import state is bound to the entire export; another export
requires a separate reviewed state directory.

A confirmed API result is recorded before advancing to the next identity. Repeat
runs skip recorded imports; they do **not** verify current PriceTag state. If an API
write has an uncertain result, the command stops and refuses to replay it on a
repeat run. Preserve the receipts for reconciliation.

## Complete the identity lifecycle

The import-only command creates identities. The [access setup](#configure-ordinary-users-budgets-and-inference-keys)
then assigns ordinary-user budgets and finite keys with separate completion
records. The coordinator runs both steps. Use normal management APIs for later
intentional changes; this test initializer does not silently repair changed roles
or rotate expired keys.

Account retirement, end-to-end identity mapping and the 10,000-user acceptance
run remain required before the combined service installer can advertise a ready
population.

## Retire imported PriceTag identities

On the gateway, use the same profile export and import-state directory:

```sh
sudo python3 tests/external-services/import-population.py \
  --profiles /root/enmaas-inputs/population-profiles.json \
  --state /root/enmaas-population-import --retire --yes
```

This deactivates only identities with confirmed import receipts and revokes their
**partner-minted keys** through PriceTag's partner API. Keys issued through other
paths require the administrator management API below; this command is not a
universal revoke-all operation. It preserves usage and audit history and does not remove
Keycloak accounts.

Retirement receipts prevent replay and accidental re-import/reactivation. An
uncertain API result stops the command for reconciliation. Keep both the export
and receipts. A local PriceTag/MaaS test verified deactivation of two identities,
revocation of one partner-minted key and a repeat run without additional writes.

Complete the gateway/key cleanup before deleting issuer accounts. Automated
issuer retirement remains unfinished; the separate full-key revocation helper below
records and verifies its completed writes.

### Revoke keys issued through other paths

After retirement, preview all remaining keys on the gateway:

```sh
sudo python3 tests/external-services/import-population.py \
  --profiles /root/enmaas-inputs/population-profiles.json \
  --state /root/enmaas-population-import --revoke-all-keys
```

Review the count, then execute:

```sh
sudo python3 tests/external-services/import-population.py \
  --profiles /root/enmaas-inputs/population-profiles.json \
  --state /root/enmaas-population-import --revoke-all-keys --yes
```

The helper uses the [management API](../../quickstarts/remote-gateway-pricetag/management-api.md#count-or-revoke-all-keys-for-a-user).
It requires confirmed retirement receipts, checks each issuer UUID and retained
login against the inactive management identity, and verifies zero active keys
after revocation. Repeated runs check the result without replaying completed writes.
Uncertain writes stop for reconciliation; keep the private receipts.

A live local test imported two identities and created both a partner-issued key
and a direct MaaS key. Partner retirement revoked only the former. This helper
then found and revoked the remaining key, verified zero active keys and repeated
without another revocation. It does not delete issuer accounts, invalidate existing
browser sessions or prevent a privileged administrator issuing new keys later.
Keep identities suspended throughout offboarding.

## Configure ordinary users' budgets and inference keys

The coordinator performs this after import. For manual setup, run on the gateway:

```sh
sudo python3 tests/external-services/import-population.py \
  --profiles /root/enmaas-inputs/population-profiles.json \
  --state /root/enmaas-population-import \
  --configure-access --monthly-usd 5 --key-lifetime 24h --yes
```

The helper requires confirmed import receipts and verifies each UUID's unique,
active login and ordinary `user` role. It uses the private management APIs for
budgets and keys; it neither creates replacement identities nor grants roles.
Keys are finite: choose `1h` or `24h`. If creation omits expiry metadata, the helper
reads back that exact active key through the identity-scoped partner API and
requires a confirmed expiry within the requested lifetime.

Each key is saved on the gateway as
`/root/enmaas-population-import/<issuer-uuid>.key`, mode `0600`. The file contains
only the key and a newline, suitable for a client's key-file input. No key value
is printed. The corresponding access receipt contains its ID, expiry and a hash
for detecting accidental file replacement; it does not duplicate the key value.

Repeat setup verifies the saved key, finite expiry and budget without issuing
another key. Changed mappings, elevated roles, expired/missing/changed keys,
preexisting unrecorded overrides and uncertain writes stop for reconciliation.
This is initialization, not a general key-rotation or budget-edit command. Use
[management operations](../../quickstarts/remote-gateway-pricetag/management-api.md)
for intentional later changes.

The local runtime test confirms two imported identities, budget readback, finite
key metadata, private file storage, no-write repeat and retirement/revocation.
It also retrieves one selected key, makes a mock inference request and verifies
exact personal request/token totals without attributing that usage to another
identity. It removes its test budget overrides afterward. Full 10,000-user access
setup, RHEL execution and load behavior still require separate acceptance evidence.

## Select an inference key

On the **gateway**, from the installed checkout, select the same population index
used for password retrieval. This prints metadata only: username, key ID, expiry
and the private key-file path.

```sh
sudo python3 tests/external-services/population-key.py --index 2
```

To emit only that key for a protected pipe or private destination, explicitly add
`--stdout`. It contains the key and a newline, suitable for OpenCode's key-file
input:

```sh
sudo python3 tests/external-services/population-key.py --index 2 --stdout
```

Without a pipe or redirection, the second command displays the key. Do not use it
in a recorded terminal. The helper verifies the export/import binding, completed
setup, private file/hash, finite expiry, active PriceTag identity and active key
through the API. Expired, revoked, changed or ambiguous keys are refused. It does
not issue, renew or rotate keys. Status can change after retrieval; normal gateway
authorization still applies to every request.

Keycloak passwords remain on the **issuer** and are used for browser login.
Inference keys remain on the **gateway** and are used by API clients. Do not
exchange the two credentials or copy either complete state directory.

## Retrieve one synthetic user's password

On the issuer, select a single population index. For example, index `2` selects
`<population-prefix>-00002`; the prefix is recorded in `population.json`.

```sh
sudo python3 tests/external-services/keycloak-password.py \
  --population /root/enmaas-test-users --index 2 --stdout
```

This explicitly prints only that user's **saved initial password**. It requires
root-private files and a completed record with the expected username and canonical
issuer UUID. It rejects out-of-range indexes, symlinks, incomplete records and
service-secret selection. It does not query or reset the current Keycloak password.

To copy it without displaying it on macOS, run from the operator workstation with
the verified SSH settings from [issuer setup](keycloak-rhel.md):

```sh
(
  set -o pipefail
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$KNOWN_HOSTS" "$KEYCLOAK_RHEL_HOST" \
    'cd ~/enmaas-mini && sudo -n python3 tests/external-services/keycloak-password.py --population /root/enmaas-test-users --index 2 --stdout' | pbcopy
)
```

Use the actual installed checkout path if different. Paste only into the expected
issuer login, then clear the clipboard with `printf '' | pbcopy`. Other platforms
can use their clipboard/password tool. Never copy the full credential directory.
For the three initial personas, use [the named-user form](keycloak-rhel.md#verify-and-connect-clients).

## Local population scale test

Run from the workstation checkout with the pinned runtime images available:

```sh
python3 tests/external-services/private-issuer-runtime.py \
  --keycloak --provisioning --population-count 10000
```

The default CI smoke test uses two users; the explicit size accepts 1–10,000.
This command uses a separate, network-isolated Keycloak with a disposable
**development database**, creates and verifies the population, repeats verification,
checks credential-free export and selected-password retrieval, then deletes the
created accounts. It prints counts and timings, never passwords. The provisioning client has a 256 MB memory limit and a 128 MB private temporary
filesystem for per-user records. Failure diagnostics include only stage, counts
and an error number; service responses and credentials remain suppressed. The larger probe
has a bounded timeout of at most one hour; temporary clients are explicitly removed
on timeout. Container secrets, network and issuer state are removed afterward.

The result measures issuer provisioning in this local fixture. It does not measure
RHEL capacity, PostgreSQL-backed Keycloak performance, PriceTag bulk import or
10,000 concurrent logins. Do not run it against a real deployment by editing its
fixed fixture endpoints.

Resource measurements are separate from the completion result. The offline
`population-report.py` helper accepts JSONL samples containing epoch `time` and
one Podman stats object in `samples`. Use the existing
[report environment](report.md) to render a reviewed sample file:

```sh
.state/report-venv/bin/python tests/external-services/population-report.py \
  --samples "$POPULATION_SAMPLES" --output "$POPULATION_REPORT"
```

The output directory must be new. CPU is computed from consecutive CPU-time
counters; memory is sampled usage and the configured limit. A chart alone does
not establish that provisioning, verification or cleanup completed successfully.
