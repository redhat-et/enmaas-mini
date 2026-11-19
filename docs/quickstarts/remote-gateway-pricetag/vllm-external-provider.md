# Self-hosted vLLM as an external provider

> **Status: field notes.** Validated once end-to-end on disposable AWS VMs
> (gateway `m7i.2xlarge` + vLLM `g6.2xlarge`, same VPC, RHEL 9.6, SELinux
> enforcing). Recorded here so the next deployment does not rediscover the
> failure modes. Nothing in the installer automates this path yet.

Serve a private vLLM model through the PriceTag gateway under the `external`
profile. Clients see `external/qwen3-8b` and keep their existing keys, budgets,
metering and dashboard; the GPU VM stays private to the gateway.

```mermaid
flowchart LR
    C[Workstation clients] -- "HTTPS :8443 user key" --> G[PriceTag gateway VM]
    G -- "HTTPS :8443 SNI: ip-…ec2.internal" --> N[nginx TLS front]
    N -- "HTTP :8000 private IP only" --> V[vLLM GPU VM]
```

The PriceTag flow has no native vLLM option (unlike the all-in-one and
remote-gateway flows, which use `scripts/common/providers enable vllm`). The
`external` profile is the integration point, and it imposes the constraints
below.

## 1. Why a TLS front end is required

Provider setup validates that external endpoints are HTTPS
(`scripts/common/provider_config.py`), but the repo's vLLM service
(`scripts/vllm`) binds plain HTTP on a private address. Terminate TLS in front
of it — nginx on the vLLM VM is sufficient:

```nginx
# /etc/nginx/conf.d/vllm.conf on the vLLM VM
server {
    listen 172.31.76.235:8443 ssl;          # vLLM VM private IPv4
    ssl_certificate     /etc/pki/tls/certs/vllm.crt;
    ssl_certificate_key /etc/pki/tls/private/vllm.key;
    location / {
        proxy_pass http://172.31.76.235:8000;  # vLLM bind address
        proxy_set_header Host $host;
        proxy_buffering off;                   # streaming responses
    }
}
```

With SELinux enforcing, allow nginx to open the upstream connection:
`sudo setsebool -P httpd_can_network_connect on`.

Security groups: grant TCP 8000 and 8443 **only from the gateway VM's security
group**. `scripts/aws/rhel-vm vllm-grant` covers 8000; add the 8443 rule
manually.

## 2. Certificates: two praxis/rustls rejections to avoid

Both failures are silent at setup time and only appear as gateway 502s.

**IP addresses are rejected as TLS SNI.** The provider endpoint must be entered
with a DNS hostname; `tls.sni` fails validation with
`sni must be a valid DNS hostname` when an IP is used. Inside AWS, the private
DNS name works and resolves in-VPC:

```
ip-172-31-76-235.ec2.internal
```

Caveat: that name encodes the current private IPv4 address. It changes on
stop/start (not reboot); a restarted vLLM VM needs its provider endpoint and
certificate SAN updated, or an Elastic IP / private hosted zone name instead.

**A self-signed leaf is rejected as `CaUsedAsEndEntity`.** curl and OpenSSL
accept a bare `openssl req -x509` certificate served directly, but praxis uses
rustls, which requires a real CA → leaf hierarchy. Generate both:

```console
sudo openssl genrsa -out /etc/pki/tls/private/vllm-ca.key 4096
sudo openssl req -x509 -new -key /etc/pki/tls/private/vllm-ca.key \
  -days 3650 -sha256 -out /etc/pki/tls/certs/vllm-ca.crt \
  -subj '/CN=vllm-internal-ca' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0'
sudo openssl genrsa -out /etc/pki/tls/private/vllm.key 2048
sudo openssl req -new -key /etc/pki/tls/private/vllm.key \
  -out /tmp/vllm.csr -subj '/CN=ip-172-31-76-235.ec2.internal'
sudo openssl x509 -req -in /tmp/vllm.csr \
  -CA /etc/pki/tls/certs/vllm-ca.crt -CAkey /etc/pki/tls/private/vllm-ca.key \
  -CAcreateserial -days 825 -sha256 -out /etc/pki/tls/certs/vllm.crt \
  -extfile <(printf 'subjectAltName=DNS:ip-172-31-76-235.ec2.internal,IP:172.31.76.235')
```

The SAN matters: rustls validates the hostname against SANs, not the CN.

## 3. Making the gateway container trust the CA

Trusting the CA on the gateway **host** (`/etc/pki/ca-trust/source/anchors/` +
`update-ca-trust`) is not enough: the praxis container loads the CA bundle from
its image, not the host.

- **Do not** bind-mount host `/etc/pki` into the container. Host CA files carry
  the SELinux `cert_t` label, the container needs `container_file_t`, and the
  cert loader **panics at startup** (`could not load certs from dir …:
  Permission denied`). Rebuilding the label with `chcon` on `/etc/pki` content
  is not a sane system change.
- **Working approach:** copy the extracted bundle into the gateway deployment
  directory — which is already mounted read-only at `/etc/praxis` — matching the
  label of the generated files, and point the container at it with
  `SSL_CERT_FILE` (honored by the container's rustls-native-certs loader):

```console
sudo install -o root -g praxis-svc -m 0640 \
  /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
  /etc/praxis-pricetag/gateway/ca-bundle.crt
sudo chcon --reference=/etc/praxis-pricetag/gateway/gateway.json \
  /etc/praxis-pricetag/gateway/ca-bundle.crt
```

Then add one line to **both** quadlet copies
(`/etc/praxis-pricetag/quadlets/pricetag-gateway.container` and
`/etc/containers/systemd/users/995/pricetag-gateway.container`), directly above
the `Exec=` line:

```ini
Environment=SSL_CERT_FILE=/etc/praxis/ca-bundle.crt
```

Reload and restart:

```console
cd /
sudo -u praxis-svc env XDG_RUNTIME_DIR=/run/user/995 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/995/bus \
  systemctl --user daemon-reload
sudo -u praxis-svc env XDG_RUNTIME_DIR=/run/user/995 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/995/bus \
  systemctl --user restart pricetag-gateway.service
```

The copied bundle is a **snapshot** of host trust: it contains the public roots
plus the vLLM CA. Re-copy it whenever the host trust store changes. Quadlet
line edits survive `providers … --replace` (the tool only rewrites `Secret=`
lines) but not a reinstall.

## 4. Add the provider to an installed gateway

Prerequisites on the gateway host, in `~/secure-single-server`:

1. Add the model preset to `configs/pricetag/provider-models.json` — the
   `openai` array is shared by direct OpenAI and external OpenAI-compatible
   setup. Use the served ID without the gateway prefix, with
   administrator-chosen limits:

   ```json
   ["qwen3-8b", 16384, 4096]
   ```

2. **First-time `external` on an active deployment has a gap**:
   `providers external --replace` assumes artifacts the installer creates. Both
   must be pre-created manually:

   - The quadlet secret slot, one line in **both** quadlet copies (it survives
     later `--replace` runs because those rewrite only `Secret=` lines):

     ```ini
     Secret=praxis-external-api-key-v1,type=env,target=CUSTOM_EXTERNAL_API_KEY
     ```

   - The state file `/etc/praxis-pricetag/provider-secrets/provider-external.json`,
     mirroring `provider-openai.json` (Podman secret name, profile `external`,
     API URLs). Create the Podman secret itself with
     `scripts/common/secret-set` or `podman secret create` under the service
     account. vLLM ignores the `Authorization` header, so a dummy key value is
     acceptable for a private deployment.

3. Run the replacement, entering the **DNS-name HTTPS** endpoint:

   ```console
   sudo python3 scripts/pricetag/providers external --api openai --replace \
     --openai-url https://ip-172-31-76-235.ec2.internal:8443
   ```

The command preserves users, budgets, usage and prices; it rewrites the
generated gateway files and restarts praxis. Failed activations roll back
(previous inputs are saved under `/root/pricetag-provider-inputs/`). Do not
edit generated `gateway.json`/`providers.json` by hand — the tooling guard
(`Native gateway settings differ from saved inputs`) then blocks future
refreshes.

## 5. Pricing and metering

No source price exists for a self-hosted model ID, so PriceTag fallback rates
apply until an administrator adds explicit ones
([pricing](pricing.md#missing-price-fallback)). Decide whether self-hosted
inference should consume user budgets at all, and set the rate in
**Administration → Model pricing** accordingly (for example a nominal $0 or an
internal rate). In the validated deployment, fallback pricing billed a
92-token thinking request $0.00582.

Metering records these requests with provider `external-openai` and the
rewritten upstream model ID, including reasoning tokens — visible per user in
the dashboard and in PostgreSQL `metering.usage_events`.

## 6. Validation tips

- **Isolate upstream TLS without auth.** A 502 from the gateway can mean DNS,
  SG, SNI or trust-store problems. To test only the container → vLLM path, run
  a throwaway praxis container with a minimal config (listener + `router` +
  `load_balancer` pointing at the vLLM DNS name — a `load_balancer` without a
  preceding router fails at startup) on the `pricetag-private` network, once
  with and once without `SSL_CERT_FILE`. With the variable set and the CA in
  the mounted bundle, `GET /v1/models` through the local listener returns the
  vLLM catalog; without it, the request fails with `Upstream TLS handshake
  failed` (UnknownIssuer). That pair of results proves whether the trust path
  is honored.
- **Qwen3 thinking consumes the token budget.** With a small `max_tokens`,
  the reply can come back with `content: null` and all tokens under
  `reasoning`. Use a larger `max_tokens`, or disable thinking via the
  chat template, before concluding the route is broken.
- **Newer OpenAI models reject `max_tokens`** in favor of
  `max_completion_tokens`; a 400 with that message is an upstream
  request-format error, not a gateway fault — and it does prove TLS and auth
  to that upstream still work.

## 7. Teardown

Follow [AWS cleanup](../../testing/aws-operations.md#cleanup): terminate both
VMs (not merely stop — stopped instances keep billing EBS storage), confirm
root volumes and interfaces are deleted, and optionally remove the tagged
security groups and imported key pairs. Client state on workstations
(`~/.config/praxis-pricetag/HOST/`) and staged provider key files
(`/root/provider-keys/`) outlive the VMs; remove them through your
secret-handling process.

## Gaps worth fixing upstream

- The PriceTag flow lacks the native vLLM provider option that the
  all-in-one/remote-gateway flows have (`scripts/common/providers enable
  vllm`), forcing the external-profile path and its HTTPS requirement.
- `providers external --replace` cannot bootstrap a first external provider on
  an already-installed gateway (quadlet slot + state file + secret).
- No supported way to add a custom upstream CA to the praxis container
  (the `SSL_CERT_FILE` + bundle-copy procedure above is a workaround).
- The external profile requires a DNS-name endpoint; an IP-literal endpoint
  could be supported with a separate SNI setting or SNI-disabled upstreams.
