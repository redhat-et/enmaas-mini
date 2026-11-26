# Private certificates for the three-host lab

Generate certificates on the administrator workstation after obtaining the VM addresses. Keep both CA signing keys on that workstation. Each VM receives only its own leaf keys and required public trust certificates.

## Generate

From the reviewed checkout on the workstation, set the three public addresses
and the observation host's private address. The private address must be covered
because the telemetry relay connects to it. Choose a new output directory:

```sh
export GATEWAY_HOST_IP=YOUR_GATEWAY_PUBLIC_IP
export KEYCLOAK_HOST_IP=YOUR_ISSUER_PUBLIC_IP
export OBSERVABILITY_HOST_IP=YOUR_OBSERVABILITY_PUBLIC_IP
export OBSERVABILITY_PRIVATE_IP=YOUR_OBSERVABILITY_PRIVATE_IP
export LAB_TLS="$PWD/.state/role-certificates"
python3 tests/external-services/certificates.py \
  --gateway "$GATEWAY_HOST_IP" \
  --keycloak "$KEYCLOAK_HOST_IP" \
  --observability "$OBSERVABILITY_HOST_IP" "$OBSERVABILITY_PRIVATE_IP" \
  --output "$LAB_TLS"
```

Use the same bundles for all three hosts; do not regenerate a different CA after
installing the issuer. For the coordinated DNS issuer, add `--keycloak-name`
with the exact selected issuer name before generating the bundles.

The output directory must not exist. Server and telemetry-client certificates expire after 30 days by default; `--days` accepts 1–30. The gateway installer requires more than seven days remaining. This is a short-lived testing PKI, without automatic renewal or revocation distribution.

| Directory | Contents and destination |
| --- | --- |
| `authorities/` | Two CA signing keys; **workstation only** |
| `trust/` | Public CA certificates; server trust and separate telemetry-client trust |
| `gateway/` | Gateway HTTPS key/certificate, issuer trust, dedicated telemetry-client key/certificate and telemetry-server trust |
| `keycloak/` | Issuer HTTPS key/certificate and public trust |
| `observability/` | Observation HTTPS key/certificate, issuer trust and telemetry-client trust |

Distinct keys identify each host. A server certificate cannot authenticate as a telemetry client. Each certificate includes its configured IP addresses and loopback names for local readiness checks.

## Optional DNS identities — preparation only

The certificate generator accepts `--gateway-name`, `--keycloak-name` and
`--observability-name` to add an exact DNS SAN to the corresponding server
certificate. Existing IP SANs remain. For example, add
`--keycloak-name identity.lab.example.com` to the generation command only after
choosing a name you control. Wildcards, URLs and unvalidated SAN strings are refused.

This creates certificate material only: it does not create DNS records, change
issuer URLs or configure private resolution. For the optional hostname issuer flow, follow [private issuer resolution](../../quickstarts/remote-gateway-pricetag/external-auth.md#browser-sign-in-with-oidc). Do not switch clients
until the issuer, callbacks and private backchannels have been configured and
tested together. Three-host RHEL qualification remains pending. An added DNS SAN does not make a private CA
publicly trusted. Existing bundles are never overwritten.

## Transfer one role bundle

Run on the workstation. Set `KNOWN_HOSTS` to a private host-key file whose
fingerprints you verified through a trusted channel. Both it and `SSH_KEY` must
be absolute paths to user-owned regular files with mode `0600`. Unlock the SSH
key in your agent if needed. `ssh-keyscan` alone does not authenticate a host.

```sh
python3 tests/external-services/transfer-client.py \
  --role keycloak --bundle "$LAB_TLS/keycloak" \
  --destination "$KEYCLOAK_RHEL_HOST" \
  --ssh-key "$SSH_KEY" --known-hosts "$KNOWN_HOSTS" &&
python3 tests/external-services/transfer-client.py \
  --role observability --bundle "$LAB_TLS/observability" \
  --destination "$OBSERVABILITY_RHEL_HOST" \
  --ssh-key "$SSH_KEY" --known-hosts "$KNOWN_HOSTS" &&
python3 tests/external-services/transfer-client.py \
  --role gateway --bundle "$LAB_TLS/gateway" \
  --destination "$GATEWAY_RHEL_HOST" \
  --ssh-key "$SSH_KEY" --known-hosts "$KNOWN_HOSTS"
```

Each destination is `user@IPv4` with noninteractive `sudo` access. The helper sends
only the selected role's allowlisted leaf keys and public certificates to
`/root/enmaas-tls`, using mode `0700` for the directory and `0600` for files.
It never sends CA signing keys, even if extra files exist in the source directory.
It disables SSH forwarding and requires an already trusted host key.

An existing destination is never overwritten. A failed transfer may leave a
partial directory: inspect it before recovery rather than rerunning blindly.
Certificate/key agreement, SANs and lifetime are checked by the consuming
installers; successful transport alone does not qualify a certificate or service.
Cross-host RHEL transfer remains an acceptance check.

## Use the bundles

The [coordinator](three-host-install.md) uses `/root/enmaas-tls`. Manual
[Keycloak](keycloak-rhel.md) and [observability](observability-rhel.md) examples
use explicit role-specific directories; substitute the transferred paths when
using this helper. Do not regenerate certificates to match a directory name. Add this option to the gateway installer running as root:

```sh
--tls-bundle /root/enmaas-tls
```

The gateway verifies certificate/key agreement, trust, server purpose, remaining lifetime and the public/loopback names before installation changes. Only its leaf certificate/key and public CA are copied into gateway state. The default installer still generates its existing private lab CA when this option is omitted.

Trust `trust/issuer-ca.pem` in test browsers and clients. Do not disable TLS verification. Trusting this CA authorizes certificates signed by its private key; protect the workstation signing keys and remove test trust after cleanup.

## Telemetry credential scope

The generated gateway bundle contains one telemetry client key/certificate. The
agent and restricted host sampler both use it. The observability receiver trusts
the dedicated telemetry CA; the issuer's HTTPS certificates do not grant ingestion
access. No telemetry CA signing key is transferred to a host.

This authenticates a trusted producer, not a PriceTag user or a particular metric
label. The receiver does not enforce certificate-subject-to-label ownership.
Sharing the client key with another service grants the same ingestion authority;
do not distribute it to harness users. Rotation must update both gateway senders.
Individual-certificate revocation is not configured by these helpers; retaining
the old trusted CA also retains trust in an unexpired old client certificate.
Plan CA/trust replacement if the gateway client key is compromised, then verify
old-key rejection and both senders' recovery. Leaf renewal is not revocation.
