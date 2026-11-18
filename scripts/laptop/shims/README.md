# Laptop shims — endpoint gaps and deterministic tokens

This directory documents (and, optionally, provides) compatibility for inference
paths that `llm-katan --backend echo` does not implement but that the PriceTag
gateway and test harnesses exercise.

## Missing endpoints

### `POST /v1/responses` (OpenAI Responses API)

* **Upstream:** llm-katan supports `POST /v1/chat/completions` and `GET /v1/models`
  only for the OpenAI provider (see [yossiovadia/llm-katan] README).
* **Harness usage:** `tests/common/provider.py` and `scripts/common/unified_config.py`
  route `INFERENCE_PATHS["openai"] = ["/v1/responses", "/v1/chat/completions"]`.
  The PriceTag performance script `scripts/pricetag/perf` sends only
  `POST /v1/chat/completions`, so the benchmark is unaffected.
* **Harness tests that hit `/v1/responses`:** `tests/rhel/unified-image.py`,
  `tests/rhel/integration.py`, `tests/rhel/responses_compat.py`, and similar
  require the full fixture (`tests/common/provider.py`), not llm-katan.

#### Mitigation

* **Preferred (laptop):** exclude the path from mock models' `apis`. The
  existing mock fixture `demo_config.mock_configuration()` already declares
  `apis=["openai","anthropic"]` but the gateway only forwards to
  `pricetag-provider:18080/18081`; with llm-katan, configure the model as
  `apis=["openai"]` + `apis=["anthropic"]` via two separate aliases only if
  you need per-API routing. For the laptop, keep the fixture model on
  `/v1/chat/completions` + `/v1/messages` — see `compose.laptop.yaml.j2`
  comments.
* **Optional shim (sidecar):** `responses_shim.py` in this directory can run as
  a tiny sidecar that translates `POST /v1/responses` -> `POST /v1/chat/completions`
  with a minimal response envelope. It is **not** started by default because it
  would hide the gap; to enable it set `ENABLE_RESPONSES_SHIM=1` in
  `configs/laptop/llm-katan.env` and add the `responses-shim` service from the
  commented block in `compose.laptop.yaml.j2`.
* **Gateway behavior:** requests to an unsupported path return 404 from the
  gateway filter chain (`Unsupported API path`), which is the correct observable
  for a missing upstream. Do not add a catch-all 200 shim that would mask
  routing errors.

### `POST /v1/messages/count_tokens` (Anthropic token-count)

* **Upstream:** llm-katan implements `POST /v1/messages` only for Anthropic.
* **Harness usage:** `scripts/common/unified_config.py` lists it in
  `INFERENCE_PATHS["anthropic"]` and the gateway explicitly excludes it from
  `token_rate_limit` / `token_count` metering (`unless: {path: "/v1/messages/count_tokens"}`).
  It is a non-billable utility endpoint; the performance harness never calls it.

#### Mitigation

* No shim required. The gateway's `token_rate_limit` and `token_count` filters
  already have `unless: {path: "/v1/messages/count_tokens"}` so ledger verification
  is unaffected. If a client needs it, run the fixture provider
  (`tests/common/provider.py`) instead of llm-katan, or add a shim similar to
  `responses_shim.py` that returns `{"input_tokens": 2}` (the fixture contract).

## Deterministic tokens (2+3=5 vs word-count)

* **Fixture contract:** `tests/common/provider.py::completion()` returns
  `usage: {prompt_tokens: 2, completion_tokens: 3, total_tokens: 5}` for **every**
  model/request — this is what `scripts/pricetag/perf verify` asserts
  (`expected = {s: [n, n*2, n*3, n*5]}`) and what `valid_reply()` checks.
* **llm-katan echo:** returns **variable word-count tokens** derived from the
  echoed text (`len(prompt.split())` etc.). It does **not** return 2+3=5.

### Strategy (adopted)

Neither patching llm-katan (an external image) nor forcing it to emit 2+3=5 is
maintainable on the laptop. Instead:

1. **Perf verify accepts variable tokens when the backend is llm-katan.** The
   `verify` step in `scripts/laptop/deploy --verify` detects llm-katan by the
   `compose.laptop.yaml` image `ghcr.io/yossiovadia/llm-katan` or by an env flag
   `PERF_VERIFY_MODE=llm-katan`. In that mode it asserts **per-user event
   counts are exact** (`count(*) = n`) but only checks that token columns are
   **positive and consistent** (`total = prompt + completion`), rather than
   exact multiples of 2/3/5. The legacy exact check remains the default for the
   fixture provider (`tests/common/provider.py`).
2. **Authoritative benchmark still uses the fixture provider.** Any published
   throughput / ledger reconciliation must run against `tests/common/provider.py`
   (or `tests/pricetag/provider.py`) so that counts match the docs. The llm-katan
   path is for local dev, dashboard inspection, and resilience injection
   (`--error-rate`, `--latency-ms`, etc.).

### What was not done

* Patching `ghcr.io/yossiovadia/llm-katan:0.22.0` to return fixed tokens would
  require maintaining a fork and rebuilding for arm64; it also hides the real
  word-count behavior that users will see. The documented dual-mode verify is
  preferred and is what CI uses.

## Running with shims explicitly

```bash
# Enable Responses shim sidecar (translates /v1/responses -> chat/completions)
ENABLE_RESPONSES_SHIM=1 scripts/laptop/deploy --mode mock --from-scratch --verify

# Verify with llm-katan token semantics (variable word-count)
PERF_VERIFY_MODE=llm-katan scripts/pricetag/perf verify \
  --report /tmp/verify/report.json --database pricetag-db --database-name metering
```

[yossiovadia/llm-katan]: https://github.com/yossiovadia/llm-katan
