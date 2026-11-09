# PriceTag gateway routing

The PriceTag gateway uses `intelligent_route` to select an upstream connection
from the request's public model alias. It uses the existing Praxis image; no
PriceTag or MaaS upgrade is required for this routing change.

## Request path

1. The HTTPS edge rejects connection upgrades and routes supported HTTP paths
   to the private OpenAI or Anthropic listener. Native listeners also reject
   upgrades before forwarding to a provider.
2. MaaS validates the user key; identity guards reject untrusted identity headers.
3. The body classifier captures the public alias in `X-Gateway-Model`. Explicit
   method, path and alias checks reject unsupported requests.
4. `intelligent_route` selects the connection from an inline candidate list.
5. Existing JSON rules rewrite the alias to the upstream model ID. Model access
   and PriceTag admission use that upstream identity; usage is charged to the
   authenticated user.
6. Credential injection supplies the selected provider's server-held key and
   the load balancer forwards the request.

Streaming Chat Completions requests have `stream_options.include_usage` set to
`true`, even if the caller omits it or sets it to `false`. This requests the
usage event needed for accounting; it preserves other options and does not add
Chat-specific fields to Responses or Messages requests. A provider that ignores
the option, or a disconnected stream without final usage, still requires
separate missing-usage handling. Successful text delivery alone is not proof of
complete accounting.

These are logical responsibilities, not a promise that every filter runs only
in textual order: JSON extraction/rewriting also uses body pre-read hooks.
Both the classifier and extractor remain because alias preservation and
caller-header sanitization depend on that behavior.

## Generated candidates

One candidate is generated per enabled alias on each native API chain:

```yaml
- filter: intelligent_route
  local_site: single-server
  model_header: X-Gateway-Model
  skip_paths: []
  candidates:
    - kind: inference_model
      name: external/model-a
      site: single-server
      cluster: external-openai
```

The inference router is **replaced**, since `intelligent_route` preserves a
previously selected cluster. Edge and dashboard path routers stay in place.
Unused protocols still authenticate and reject inference; they get no candidates.

The pinned [Praxis AI filter](https://github.com/redhat-et/praxis-ai/blob/0b35bf3ebdff0d5d924f8346c917f42440defc1f/filters/src/routing/intelligent_route.rs)
selects clusters but does not rewrite the request model. Its
[candidate schema](https://github.com/redhat-et/praxis-ai/blob/0b35bf3ebdff0d5d924f8346c917f42440defc1f/filters/src/routing/descriptor.rs)
has no `target_model`; its filter configuration has no `body_authoritative`.
Keep the generated rewrite rules when using this image.

## Boundaries

- Provider setup, aliases, catalog limits, prices, keys and activation commands
  stay as documented in [Providers and models](../quickstarts/remote-gateway-pricetag/providers.md).
- Different aliases can select different connections for the same upstream ID.
  Their credentials remain separate; PriceTag's model-price identity remains
  the upstream ID, so this does not introduce supplier-specific prices.
- Shared all-in-one deployments retain their explicit router. The pinned
  pipeline validator does not recognize `intelligent_route` as the guaranteed
  logical-upstream binder required by their token-limit conditions.
- No routing overlays, hot reload, provider-management API or change to the
  public model catalog's authentication is introduced here.

[Mock deployment checks](../testing/pricetag-demo-ci.md) cover protocol routing,
credential separation, alias spoofing, accounting and authorization. Wider
provider management remains follow-up work in
[enMAAS Mini #3](https://github.com/redhat-et/enmaas-mini/issues/3).
