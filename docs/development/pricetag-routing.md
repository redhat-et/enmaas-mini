# PriceTag gateway routing

The PriceTag gateway uses `model_to_provider` to select an upstream connection
and rewrite the request's public model alias. It uses the existing Praxis image;
PriceTag normalizes the reported alias to the configured upstream model ID.

## Request path

1. The HTTPS edge routes supported paths to the private OpenAI or Anthropic listener.
2. MaaS validates the user key; identity guards reject untrusted identity headers.
3. The body classifier captures the public alias in `X-Gateway-Model`. Explicit
   method, path and alias checks reject unsupported requests.
4. `model_to_provider` selects the connection and rewrites the alias to the
   upstream model ID. Model access and PriceTag admission use that upstream
   identity; usage is charged to the authenticated user.
6. Credential injection supplies the selected provider's server-held key and
   the load balancer forwards the request.

These are logical responsibilities, not a promise that every filter runs only
in textual order: JSON extraction/rewriting also uses body pre-read hooks.
Both the classifier and extractor remain because alias preservation and
caller-header sanitization depend on that behavior.

## Generated mappings

One mapping is generated per enabled alias on each native API chain:

```yaml
- filter: model_to_provider
  models:
    - model: external/model-a
      upstream: external-openai
      target_model: provider-model-a
      paths:
        - /v1/responses
        - /v1/chat/completions
```

Edge and dashboard path routers stay in place. The native inference router and
its per-model JSON rewrites are replaced by one `model_to_provider` mapping.
Unused protocols still authenticate and reject inference; they get no mappings.

The pinned [Praxis AI filter](https://github.com/redhat-et/praxis-ai/blob/0b35bf3ebdff0d5d924f8346c917f42440defc1f/filters/src/inference/model_to_provider.rs)
publishes the public model ID for `external_metering` before the balance check
and rewrites the selected upstream body. PriceTag uses its mounted gateway
catalog to normalize that public alias for pricing and quota decisions.

## Boundaries

- Provider setup, aliases, catalog limits, prices, keys and activation commands
  stay as documented in [Providers and models](../quickstarts/remote-gateway-pricetag/providers.md).
- Different aliases can select different connections for the same upstream ID.
  Their credentials remain separate; PriceTag's model-price identity remains
  the upstream ID, so this does not introduce supplier-specific prices.
- Shared all-in-one deployments retain their explicit router; this replacement
  applies only to the standalone PriceTag gateway chains.
- No routing overlays, hot reload, provider-management API or change to the
  public model catalog's authentication is introduced here.

[Mock deployment checks](../testing/pricetag-demo-ci.md) cover protocol routing,
credential separation, alias spoofing, accounting and authorization. Wider
provider management remains follow-up work in
[enMAAS Mini #3](https://github.com/redhat-et/enmaas-mini/issues/3).
