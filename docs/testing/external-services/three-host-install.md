# Install the three-host test deployment

Coordinate a gateway, a Keycloak issuer and an observability host using the
existing RHEL installers. This testing path uses mock inference, a private CA
and explicit SSH host trust. It does not provision infrastructure or qualify
public access. The [gateway quickstart](../../quickstarts/remote-gateway-pricetag/README.md)
remains the production installation entry point.

## Prepare the hosts and trust

- Use three fresh RHEL hosts with distinct public and private IPv4 addresses,
  a non-root SSH account and passwordless `sudo`.
- Permit SSH from your workstation only. Permit workstation HTTPS on TCP 8443;
  allow issuer TCP 8443 between the private peers and collector TCP 24443 from
  the gateway to observability. Do not expose the collector publicly.
- Provide a DNS name for the issuer resolving only to its public IPv4 on your
  workstation. The services use the same TLS name with private address mapping.
- Verify each SSH host fingerprint through a trusted channel and put only
  those verified keys in a dedicated known-hosts file. `ssh-keyscan` alone does
  not establish trust. Keep that file and the SSH key user-owned with mode 600.
- Use a clean, reviewed, published repository commit containing this coordinator.
  Apply fetches that exact commit on each host. No credentials belong in Git.

## Create the deployment configuration

From the repository checkout on your workstation, set these nonsecret values.
The state directory and certificate destination must be new. Do not reuse a
deployment ID or copy another deployment's receipts.

```bash
export GATEWAY_SSH=ec2-user@YOUR_GATEWAY_IP
export KEYCLOAK_SSH=ec2-user@YOUR_KEYCLOAK_IP
export OBSERVABILITY_SSH=ec2-user@YOUR_OBSERVABILITY_IP
export GATEWAY_PRIVATE_IP=YOUR_GATEWAY_PRIVATE_IP
export KEYCLOAK_PRIVATE_IP=YOUR_KEYCLOAK_PRIVATE_IP
export OBSERVABILITY_PRIVATE_IP=YOUR_OBSERVABILITY_PRIVATE_IP
export ISSUER_NAME=YOUR_ISSUER_DNS_NAME
export WORKSTATION_CIDR=YOUR_WORKSTATION_IP/32
export SSH_KEY="$HOME/.ssh/YOUR_KEY"
export KNOWN_HOSTS="$HOME/.ssh/YOUR_VERIFIED_HOSTS_FILE"
export LAB_STATE="$PWD/.state/three-host"
umask 077
python3 - <<'PY'
import json, os, secrets, subprocess
from pathlib import Path
root = Path(os.environ['LAB_STATE'])
root.mkdir(mode=0o700, parents=True, exist_ok=False)
deployment = 'em-lab-' + secrets.token_hex(6)
config = {
    'version': 1, 'deployment': deployment,
    'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
    'source_repository': 'https://github.com/redhat-et/enmaas-mini.git',
    'repository': '/opt/enmaas-mini/' + deployment,
    'ssh_key': os.environ['SSH_KEY'], 'known_hosts': os.environ['KNOWN_HOSTS'],
    'tls_directory': str(root / 'tls'), 'issuer_name': os.environ['ISSUER_NAME'],
    'workstation_cidr': os.environ['WORKSTATION_CIDR'],
    'external_database': False, 'population_count': 20,
    'hosts': {role: {'ssh': os.environ[role.upper() + '_SSH'],
                     'private_ip': os.environ[role.upper() + '_PRIVATE_IP']}
              for role in ('keycloak', 'observability', 'gateway')},
}
(root / 'services.json').write_text(json.dumps(config, indent=2) + '\n')
print('Configuration:', root / 'services.json')
print('Deployment:', deployment)
PY
```

For a reviewed fork, change `source_repository` to its public GitHub
`enmaas-mini.git` URL. The selected SHA must be published there. For external
databases, first follow [external PostgreSQL](../../quickstarts/remote-gateway-pricetag/external-database.md)
to place the protected URL and CA files on the gateway, then set
`external_database` to `true`. This coordinator does not create databases.

## Preview, then install

Preview performs no network access or host changes:

```bash
python3 tests/external-services/services.py --config "$LAB_STATE/services.json"
```

Review the commands and hosts. Apply requires the exact deployment confirmation:

```bash
DEPLOYMENT_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deployment"])' "$LAB_STATE/services.json")
python3 tests/external-services/services.py --config "$LAB_STATE/services.json" \
  --state "$LAB_STATE/install-receipts" --confirm "install $DEPLOYMENT_ID"
```

The coordinator generates role-specific certificates, installs issuer and
observability, builds the gateway's pinned OIDC-capable sources, connects
telemetry, imports admin/Alice/Bob and creates the requested test population
(1–10,000 users). Alice manages Bob. Population users receive a USD 5 monthly
allowance and 24-hour inference keys. The three dashboard personas do not receive
new inference keys through this step. Browser passwords and inference keys are
different credentials.

The pinned metering source is
[2281e1a](https://github.com/Ladas/pricetag-metering/commit/2281e1a84bfc3b998e7db36bc01bae88130bfcdd),
matching the [OIDC guide](../../quickstarts/remote-gateway-pricetag/external-auth.md).
The coordinator supplies this exact revision explicitly rather than relying on
future installer defaults.

## Connect and verify

```bash
python3 tests/external-services/services.py --config "$LAB_STATE/services.json" --access
```

This prints configured URLs, the public CA path and verified SSH commands for
selected passwords/keys and provider management. **The listing contains no
credential values.** Commands marked `--stdout` or `cat` reveal a credential
when you run them; use a protected destination, not a recorded terminal.

Trust the generated public CA explicitly in your browser/client. Keep the
`authorities/` directory private on the workstation; it is never transferred to
the hosts. The generated certificates expire after 30 days; automatic renewal
is not provided.

Installation reports `acceptance_verified: false`. Before sharing access,
verify issuer login, admin/Alice/Bob role boundaries, authenticated inference,
rejection without a key, populated monitoring and private-only ingestion. See
the [issuer](keycloak-rhel.md), [observability](observability-rhel.md) and
[monitoring](monitoring.md) checks. Local container tests do not qualify RHEL
SELinux, reboot behavior, real routing or cloud firewall enforcement.

## Recover a stopped installation

Completed steps have private durable receipts and are skipped on a repeat run
with the identical configuration. A failed or uncertain step stops the run;
it is never retried automatically. Inspect the selected host and receipts
before deciding how to reconcile. Do not delete receipts or mark a step complete
merely to bypass the check. Hosts with pre-existing installation state are
refused. This is a fresh-install coordinator, not an upgrade or uninstall tool.
