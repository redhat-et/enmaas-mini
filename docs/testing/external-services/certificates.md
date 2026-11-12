# Private certificates for the three-host lab

Generate certificates on the administrator workstation after obtaining the VM addresses. Keep both CA signing keys on that workstation. Each VM receives only its own leaf keys and required public trust certificates.

## Generate

From the repository on the workstation, run `aws_lab_info` after deployment. It exports the public and private addresses used below. Include the observation host’s private IP because the telemetry relay connects to that address.

```sh
aws_lab_info
export LAB_TLS="$HOME/.config/enmaas-mini/lab-tls"
python3 tests/external-services/certificates.py \
  --gateway "$GATEWAY_HOST_IP" \
  --keycloak "$KEYCLOAK_HOST_IP" \
  --observability "$OBSERVABILITY_HOST_IP" "$OBSERVABILITY_PRIVATE_IP" \
  --output "$LAB_TLS"
```

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
issuer URLs or configure private resolution. For the optional hostname issuer flow, follow [private issuer resolution](oidc.md#private-issuer-resolution). Do not switch clients
until the issuer, callbacks and private backchannels have been configured and
tested together. Three-host RHEL qualification remains pending. An added DNS SAN does not make a private CA
publicly trusted. Existing bundles are never overwritten.

## Transfer one role bundle

Use the matching verified SSH host and SSH key. Check the host fingerprint before the first connection. This example transfers the gateway bundle; repeat with `keycloak` and `observability` and their corresponding host variables.

```sh
ROLE=gateway
TARGET="$GATEWAY_RHEL_HOST"
tar -C "$LAB_TLS/$ROLE" -cf - . | \
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes "$TARGET" \
  "sudo sh -c 'umask 077; mkdir /root/enmaas-tls && tar -xf - -C /root/enmaas-tls'"
```

The remote directory must not already exist. Never transfer the parent `lab-tls` directory or `authorities/`. Leaf private keys are deployment credentials; do not include them in test evidence or Git.

## Use the bundles

The [Keycloak](keycloak-rhel.md) and [observability](observability-rhel.md) commands use `/root/enmaas-tls` directly. Add this option to the gateway installer running as root:

```sh
--tls-bundle /root/enmaas-tls
```

The gateway verifies certificate/key agreement, trust, server purpose, remaining lifetime and the public/loopback names before installation changes. Only its leaf certificate/key and public CA are copied into gateway state. The default installer still generates its existing private lab CA when this option is omitted.

Trust `trust/issuer-ca.pem` in test browsers and clients. Do not disable TLS verification. Trusting this CA authorizes certificates signed by its private key; protect the workstation signing keys and remove test trust after cleanup.
