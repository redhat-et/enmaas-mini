# Headless installation

Run from the copied checkout on a **fresh RHEL host**. `--non-interactive` prevents
prompts even when the session has a terminal. A real installation also requires
`--yes`; incomplete provider settings fail before installation. Existing deployment
checks and credential-file validation still apply.

## Preview

Set the gateway hostname or public IP. A plan reads no provider credentials and
does not install packages, pull images or start services.

```console
GATEWAY_HOST=YOUR_GATEWAY_HOST_OR_IP
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --provider openai --non-interactive --plan
```

## Install with a provider

Prepare the provider key on this host using the protected-file procedure in
[provider management](providers.md). Pass the **absolute file path**, never the key
value. The file must be owned by the invoking administrator and private.

```console
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --provider openai --openai-key-file /root/openai.key \
  --image-mode published --no-sample-users --non-interactive --yes
```

This creates the administrator only. Omit `--no-sample-users` to create Alice and
Bob too. Continue with [dashboard access](README.md#4-open-the-dashboards) and
[user management](users.md).

For an external provider, specify its protocol and each selected endpoint:

```console
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --provider external --external-api both \
  --external-openai-url https://provider.example/v1 \
  --external-messages-url https://provider.example \
  --external-key-file /root/external.key \
  --image-mode published --no-sample-users --non-interactive --yes
```

Replace the example endpoints. Use `--external-api openai` or `anthropic` and its
corresponding URL if the provider exposes only one API. To install both providers,
combine their flags in one command; do not run the installer twice on one host.

## Mock installation

For tests with synthetic responses and no provider credentials:

```console
sudo scripts/pricetag/install --hostname "$GATEWAY_HOST" \
  --mode mock --image-mode published --non-interactive --yes
```

`--non-interactive` controls prompts; `--mode mock` selects synthetic provider
profiles. This command does not install Keycloak or an
observability stack. Use the [manual installation](manual.md) guide when you need
to run individual stages yourself.
