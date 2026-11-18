# Dedicated RHEL Keycloak test host

**RHEL qualification pending.** The generated Quadlets started as rootless systemd services in a local Podman Linux VM. Issuer discovery, public admin/master rejection, traversal probes and persona logins through Praxis passed there. RHEL firewalld, SELinux, reboot recovery and the complete installer still require testing. Use a disposable test VM.

## Host prerequisites

Use the reviewed repository checkout on this host. Install the tools needed to generate inputs before running the installer:

```sh
sudo dnf install -y python3 python3-pyyaml openssl
```

## Prepare

Use the Keycloak VM from the three-host AWS lab. Keep SSH restricted to your workstation. TCP 8443 must be reachable from the workstation, gateway and observability hosts. This installer changes the host firewall, not AWS security groups.

From the repository on the Keycloak host, prepare protected inputs with the three public IPv4 addresses:

```sh
sudo python3 tests/external-services/keycloak-host.py \
  --issuer "$KEYCLOAK_HOST_IP" \
  --gateway "$GATEWAY_HOST_IP" \
  --observability "$OBSERVABILITY_HOST_IP" \
  --output /root/enmaas-keycloak-inputs
```

The generator creates a dedicated realm, immutable UUIDs for three test users, separate gateway/observability client secrets, and Quadlet definitions. Passwords remain in the private `credentials.json`; they are not printed. Existing inputs are never overwritten.

The initial realm contains only the three personas. **The full 10,000-user setup is not ready yet.** The [issuer population command](keycloak-users.md)
passed a local 10,000-user test with a disposable development database.
PostgreSQL-backed RHEL execution, cross-host PriceTag mapping and account lifecycle
acceptance remain pending. Do not enlarge `realm.json` to carry it:
[Podman limits individual secrets](https://docs.podman.io/en/latest/markdown/podman-secret-create.1.html).
Preflight rejects empty secrets or files above the conservative 500,000-byte bound
before host changes. Bulk users need a resumable Keycloak administrative API step,
followed by matching immutable-identity provisioning through PriceTag's API.
Increasing the import size does not provide that identity mapping.

Supply a matching server certificate/key and public issuer CA, owned by root with mode `0600`. The server certificate must cover the issuer IP and `localhost` (the loopback administration endpoint). Generate/sign certificates on the workstation: do not copy the CA signing key to this VM.

For a DNS issuer with private peer connections, follow the optional
[private issuer resolution](oidc.md#private-issuer-resolution)
configuration before generating inputs. Keep both clients on the same issuer name.

## Install

Run from the repository on the issuer host. List narrow source networks for the workstation and the two peer VMs; use the addresses actually seen by the host firewall.

```sh
sudo tests/external-services/install-keycloak \
  /root/enmaas-keycloak-inputs \
  /root/enmaas-tls/server.pem \
  /root/enmaas-tls/server.key \
  /root/enmaas-tls/ca.pem \
  "$WORKSTATION_CIDR" "$GATEWAY_SOURCE_CIDR" "$OBSERVABILITY_SOURCE_CIDR"
```

The service account is `praxis-svc`. Containers are rootless, capability-dropped and managed by its user systemd instance with lingering enabled. Database state is a named volume. Application credentials are imported as Podman secrets; only their consuming containers receive them.

| Endpoint | Boundary |
| --- | --- |
| Public HTTPS `8443` | Praxis allows `/realms/enmaas-lab/` and `/resources/`; other routes return 404. Source IP restrictions remain in AWS and firewalld. |
| Keycloak administration HTTPS `127.0.0.1:18443` | Host-local administration over SSH; no public admin route. No SSH tunnel is required by this runbook. |
| Keycloak HTTP `8080` | Container network only, consumed by the local HTTPS proxy. |
| PostgreSQL `5432` | Container network only; no host port. |

Do not use the temporary bootstrap administrator as a permanent operating account. The administrative account setup and recovery procedure still need qualification. The three realm users and passwords are test fixtures, not production onboarding.

## Connect the other services

The generator creates separate gateway and observability client files. Transfer
only those files from the **operator workstation** with the helper below. It checks
source ownership/permissions and successful completion before sending data, then
installs the destination atomically without replacing existing files. Secret values
are never printed or put in command arguments.

Use a private `KNOWN_HOSTS` file containing host keys whose fingerprints you have
verified through a trusted channel. `ssh-keyscan` alone does not authenticate a
host. The helper uses strict host-key checking and noninteractive SSH; unlock the
selected SSH key in your agent beforehand if necessary.

Prepare `/root/enmaas-inputs` on each consuming host, owned by root with mode
`0700`, using the same verified SSH connection. Then run:

```sh
python3 tests/external-services/transfer-client.py \
  --issuer "$KEYCLOAK_RHEL_HOST" --destination "$GATEWAY_RHEL_HOST" \
  --client gateway --ssh-key "$SSH_KEY" --known-hosts "$KNOWN_HOSTS" &&
python3 tests/external-services/transfer-client.py \
  --issuer "$KEYCLOAK_RHEL_HOST" --destination "$OBSERVABILITY_RHEL_HOST" \
  --client observability --ssh-key "$SSH_KEY" --known-hosts "$KNOWN_HOSTS"
```

Each host argument is `user@IPv4`. Both local SSH files must be user-owned,
regular files at absolute paths with mode `0600`. The helper reads only the fixed
client files, never the issuer's full credential file or bootstrap password. It
does not change host trust or provision directories.

On the gateway, copy its existing **public issuer CA** into the path referenced by
the OIDC file:

```sh
sudo sh -c 'test ! -e /root/enmaas-inputs/issuer-ca.pem && install -m 0600 /root/enmaas-tls/issuer-ca.pem /root/enmaas-inputs/issuer-ca.pem'
```

If SSH fails or times out, inspect the destination before retrying: a completed
write may have lost its acknowledgement. Existing files are never overwritten.
The helper's remote read/write contract is tested locally; actual SSH/sudo and
RHEL acceptance remain part of the three-host qualification.

Continue with [gateway OIDC setup](oidc.md) and the [observability installer](observability-rhel.md). Provision the UUIDs recorded in `identities.json` through the partner API before attempting gateway SSO; matching email addresses alone do not establish identity. Observability uses its separate client and explicit administrator group.

## Retrieve one test-user password

Run on the issuer host from the installed checkout. This prints only the selected
user's **saved initial password**, and only when `--stdout` is supplied. The input
directory must be root-owned with mode `0700`; both JSON files must be root-owned
with mode `0600`. Symlinked input directories and files are rejected:


```sh
sudo python3 tests/external-services/keycloak-password.py --user alice --stdout
```

Replace `alice` with another identity in `identities.json`. The helper rejects
service-secret names and the bootstrap administrator. It does not reset a password
or query its current value from Keycloak. If a user has changed their password,
the saved initial value is no longer a login credential.

To copy Alice's saved password on macOS without displaying it, run from the
operator workstation with the verified SSH settings used above:

```sh
(
  set -o pipefail
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$KNOWN_HOSTS" "$KEYCLOAK_RHEL_HOST" \
    'cd ~/enmaas-mini && sudo -n python3 tests/external-services/keycloak-password.py --user alice --stdout' | pbcopy
)
```

Use the installed checkout path if it differs. Paste only into the expected test
issuer login, then clear the clipboard with `printf '' | pbcopy`. Other platforms
can pipe the same SSH output to their clipboard/password tool. Do not print the
entire `credentials.json` or paste passwords into test reports.

## Inspect or recover

```sh
sudo -u praxis-svc env \
  XDG_RUNTIME_DIR="/run/user/$(id -u praxis-svc)" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u praxis-svc)/bus" \
  systemctl --user status lab-keycloak-db.service lab-keycloak.service \
  lab-keycloak-proxy.service --no-pager
```

The installer refuses an existing `/etc/enmaas-keycloak-lab` marker, unit or secret. After a partial failure, inspect the services and inputs rather than deleting state or recreating credentials. Do not publish raw logs or private input files.

After startup, verify the public boundary from a permitted source:

```sh
python3 tests/external-services/keycloak-boundary.py \
  --url "https://${KEYCLOAK_HOST_IP}:8443" --ca /path/to/issuer-ca.pem
```

For local ARM testing, use a native Praxis image with the same verified source revision; an AMD64 image under emulation failed upstream connections with an unsupported socket option. Do not treat an emulated-image failure as a RHEL result.

Before RHEL acceptance, verify certificate/SAN checks, exact issuer discovery, public admin and master-realm denial (including encoded/normalized paths), all persona logins, restricted peer access, reboot recovery and secret/certificate rotation. The local shared-pod tests do not replace these checks.

## Existing firewall configuration

Before creating service secrets, the installer audits runtime and permanent
firewalld rules. It repeats the audit before starting services. Extra ports,
services, unreviewed policies, direct rules and broader rich rules stop the
installation. Existing rules are never removed automatically. The standard
IPv6 host policy is accepted only with its reviewed ICMPv6 behavior; SSH and
DHCPv6 service definitions are checked rather than trusted by name.

If the audit stops, review the named configuration on the dedicated VM before
retrying. Do not disable firewalld or remove the IPv6 policy to bypass the check.
This audit does not cover independently installed nftables rules or replace AWS
security-group checks and external packet-level deny probes. Its live RHEL
qualification is still pending.

## Login abuse protection

Newly generated realms disable self-registration and password-reset flows and
configure temporary brute-force lockout: five failed logins, a 60-second wait
increment and a 15-minute maximum wait. Quick repeated failures can also trigger
a temporary wait. Existing imported realms are not updated by regenerating files;
realm import does not provide an upgrade mechanism.

The pinned image passed isolated login-form tests for valid credentials, wrong
passwords, temporary lockout and recovery. Repeat these checks against the
PostgreSQL-backed RHEL issuer and test concurrent attempts before public access. Account-level lockout
can itself be abused to deny someone access; it complements request limits and
administrator MFA rather than replacing them. Administrator MFA enrollment remains
a public-access prerequisite to design and qualify.

See [Keycloak brute-force protection](https://www.keycloak.org/docs/26.8.0/server_admin/#password-guess-brute-force-attacks).

Run the isolated local lockout test with Podman:

```sh
python3 tests/external-services/issuer-lockout.py
```

It generates a separate realm and TLS material, publishes only a random loopback
port, checks login/failure/lockout/recovery, then removes its container and secrets.
It does not modify the running demo or use its credentials. Allow roughly two
minutes; the test waits for the actual lockout period rather than shortening the
configured policy. This local fixture uses the pinned Keycloak image with its
embedded development database; RHEL's PostgreSQL-backed issuer remains a separate
qualification target.
