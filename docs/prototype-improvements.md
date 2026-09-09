# Prototype improvements

## Connection ownership

Normal FastAPI lifespan creates a separate HTTPX pool for each provider in
`providers/connections.py`, stored on that application instance. Requests still
set their credentials explicitly. Borrowed handles implement the subset of HTTPX
operations adapters use; closing a handle does not close its shared pool. Stream
generators close their response under a cancellation shield. Application shutdown
closes both pools. Standalone adapter tests retain the previous factory interface.

Limits: 100 connections and 20 idle keep-alive connections per provider per
process; existing 60-second HTTPX timeout remains. This is not a global quota or
multi-process concurrency limit. Provider cookies are disabled using the pinned
HTTPX client's cookie jar; its private `_cookies` integration has a regression
test and must be reviewed when HTTPX is upgraded.

## Explicit Anthropic settings

The adapter now accepts only translated top-level fields: model, messages,
max_tokens or max_completion_tokens (not both), stream, stop, and stream_options
with include_usage=true. Other top-level settings return a 400 explaining the
gateway adapter limitation. This does not claim that the upstream model lacks
these features. Fireworks still forwards its OpenAI-shaped fields upstream.

Validation runs before cache lookup as well as at adapter entry. A cached answer
cannot conceal unsupported settings. For Anthropic cache opt-in use cache=true
without temperature; the proxy removes its private cache flag before validation.
This intentionally changes requests that previously silently dropped parameters.

## Verification

The black-and-white dashboard was opened in a browser and its refresh action
successfully updated the snapshot. This was desktop verification, not a complete
mobile or accessibility audit.

An isolated 8-client, 30-second localhost pooled run completed all 80 requests:
p50 49.569 ms, p95 69.470 ms, p99 78.148 ms. The earlier non-pooled run had p50
80.567 ms and p95 133.668 ms. These single, non-interleaved mock runs suggest a
latency improvement, not a statistically established production speedup.
Throughput remains approximately 2.67 successful requests/sec because the
generator intentionally paces requests. No maximum-capacity claim is warranted.

Reproduce the pooled run with:

```bash
python -m client_testing.http_load --pooled --output benchmarks/results/local-http-pooled.json
```

Restart a running non-reloading proxy to activate lifespan changes. No real
provider traffic was required for this verification.
