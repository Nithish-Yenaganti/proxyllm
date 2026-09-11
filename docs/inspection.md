# Request inspection

`POST /v1/inspect` previews one request for one configured model without contacting
an LLM provider. It accepts the same JSON body and virtual-key authorization as
`POST /v1/chat/completions`.

Start the normal gateway using the README setup, then inspect a request:

```bash
curl -sS http://127.0.0.1:8000/v1/inspect \
  -H "Authorization: Bearer $PROXY_VIRTUAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"anthropic/claude-sonnet-5","messages":[{"role":"system","content":"Be helpful."},{"role":"user","content":"Hello"}],"max_tokens":50,"temperature":0.2}'
```

This returns HTTP 400 with `status: blocked`, a null `payload`, and an error naming
`temperature`, which this gateway's Anthropic adapter does not implement. Remove
that field and repeat: HTTP 200 reports `status: prepared`, the upstream model,
the translated `payload`, and a list of top-level fields added, removed, or changed.
The system instruction appears in the Anthropic `system` field. The gateway-only
`cache` field, when supplied, is removed. Only the first adapter validation error
is returned; routing, authentication, and configuration errors use the existing
OpenAI-style `error` envelope.

`prepared` means the configured adapter constructed the payload, not that the
upstream service accepted it. `upstream_verified` is always false. Fireworks
forwards most fields without validating model-specific capabilities; inspection
preserves this behavior. No model catalog or remote capability probe is used.

Both adapters expose `prepare()` and call it from `send()`. The inspector calls
that same method. No credentials or provider URL are included in the report;
the payload contains the caller's own prompt, so responses use `Cache-Control:
no-store`. Inspection does not persist prompts, create usage records, or read or
write the response cache. It still consumes the shared 24-request rolling rate
limit and requires a configured provider credential and permission for that
provider. These checks do not validate the credential remotely.

## Reproducible demonstration

```bash
.venv/bin/python -m benchmarks.inspection_demo
```

This starts the actual gateway handlers and authentication middleware in-process,
with a temporary SQLite database, an ephemeral virtual key, and an HTTPX mock
provider. It opens no listening port, uses no real provider credentials, and
removes its temporary database on exit.

The demo prints a blocked inspection, a corrected prepared payload, and a
successful chat execution. It asserts there were zero provider calls during
inspection and that the one outbound execution body exactly matches the preview.
Expected last line:

```text
PASS: zero provider calls during inspection; one mock call during execution; payloads match.
```

## Verification

```bash
.venv/bin/python -m unittest tests.test_inspection -v
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover both providers and streaming settings, HTTP body parity, input
immutability, consistent adapter errors, authentication and revocation, provider
permissions, shared throttling, malformed/oversized requests, and absence of
provider connections, usage writes, and cache activity during inspection.

A real-provider call is separate from inspection and may incur charges. To test
one deliberately, send the same corrected body to `/v1/chat/completions` on your
normally configured gateway. The included demo establishes local payload parity,
not live upstream acceptance or model answer quality.
