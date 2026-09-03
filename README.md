# ProxyLLM

ProxyLLM is a self-hosted, OpenAI-compatible gateway for applications that use multiple large language model (LLM) providers. An application sends one familiar `/v1/chat/completions` request to ProxyLLM, and the gateway authenticates it, selects the configured provider, forwards the request, and returns a consistent response.

## What does it do?

ProxyLLM provides one controlled entry point for Fireworks and Anthropic models. It includes:

- virtual API keys, so applications never receive the real provider credentials;
- per-key provider permissions, stored in SQLite;
- model-based routing through one OpenAI-compatible endpoint;
- normal and Server-Sent Events (SSE) streaming responses;
- usage, cost, latency, and outcome logging without storing prompts or answers;
- an optional one-hour cache for deterministic non-streaming requests; and
- benchmark tools for measuring gateway overhead, cache savings, and load behavior.

## Why does it exist?

Using provider APIs directly becomes difficult when several applications, developers, or models share the same credentials. Provider-specific request formats spread into client code, credentials become harder to rotate safely, access is difficult to revoke per application, and usage is fragmented across services.

ProxyLLM puts those concerns in one local gateway. Clients use a stable API and a revocable virtual key, while the gateway owns provider credentials, routing rules, permissions, and measurement.

## How is it helpful?

- **Safer credential management:** real Fireworks and Anthropic keys remain on the gateway.
- **Simpler client integration:** OpenAI-compatible clients can use one base URL while switching models through the `model` field.
- **Fine-grained access:** each virtual key can be granted or denied access to individual providers.
- **Easier provider changes:** routing and provider-specific translation stay in the gateway instead of every client.
- **Useful cost visibility:** each request records tokens, estimated cost, latency, cache status, and outcome.
- **Lower repeated-request cost:** eligible deterministic responses can be reused within the same virtual-key boundary.
- **Measurable performance:** included benchmarks help validate overhead, cache effectiveness, and supported load.

## Who is it for?

ProxyLLM is useful for:

- developers running local tools or AI applications against more than one provider;
- small teams that need separate, revocable credentials for each application;
- platform engineers evaluating a lightweight LLM gateway architecture;
- projects that need provider-independent streaming and response formats; and
- engineers who want request-level usage and cost estimates without retaining user content.

It is currently best suited to local development, prototypes, portfolio projects, and controlled internal deployments. It is not yet a complete production gateway: it has no provider fallback, distributed cache, high-availability database, management API, or built-in rate limiting.

## How it works

```text
OpenAI-compatible client
        |
        | Bearer virtual-key + requested model
        v
ProxyLLM authentication and permission check
        |
        | model route + server-side provider credential
        v
Fireworks or Anthropic
        |
        | normalized complete or streaming response
        v
Client + private usage record in SQLite
```

For a file-by-file explanation of the implementation, see [`CODE_GUIDE.md`](CODE_GUIDE.md).

## Quick start

### 1. Install dependencies

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. Configure at least one provider

Copy `.env.example` to an ignored `.env`, then fill in only the credentials you intend to use.

```bash
cp .env.example .env
```

```text
FIREWORK_API_KEY=...
FIREWORK_URL=https://api.fireworks.ai/inference/v1/chat/completions
ANTHROPIC_API_KEY=...
ANTHROPIC_URL=https://api.anthropic.com/v1/messages
```

Never commit or display `.env`.

### 3. Initialize or migrate SQLite

```bash
python -m auth.database
```

Existing Phase 2 keys automatically keep their original provider permission.

The same startup migration also creates `usage_logs`, adds cache metrics to older logs, and creates the Phase 5.2 `response_cache` table.

### 4. Create a virtual key

```bash
# Create a key with its first Fireworks permission.
python -m auth.cli create --app jan --provider fireworks
```

The complete virtual key is displayed only once when it is created.

### 5. Run the gateway

```bash
uvicorn api.main:app --reload
```

The gateway is now available at `http://127.0.0.1:8000`.

## Virtual-key management

Use the CLI to inspect permissions, add another provider, or revoke a key:

```bash
# Safely list key IDs, prefixes, status, and provider permissions.
python -m auth.cli list

# Grant an existing key access to Anthropic.
python -m auth.cli grant --id 1 --provider anthropic --credential default

# Revoke the complete key and all of its provider access.
python -m auth.cli revoke --id 1
```

## Configured public models

```text
accounts/fireworks/models/deepseek-v4-flash-0731
fireworks/deepseek-v4-flash
anthropic/claude-sonnet-5
claude-sonnet-5
```

## Test a normal request

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic/claude-sonnet-5",
    "messages": [{"role": "user", "content": "Reply with hello"}],
    "max_tokens": 100
  }'
```

## Test a streaming request

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_VIRTUAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic/claude-sonnet-5",
    "messages": [{"role": "user", "content": "Count from one to five"}],
    "stream": true,
    "max_tokens": 150
  }'
```

## Run automated tests

```bash
python -m unittest discover -s tests -v
```

Automated tests use temporary SQLite databases and in-memory HTTP providers, so they do not read real keys or spend provider credit.

## Usage logging

Every authenticated chat request writes one row containing the virtual-key ID, provider, public model, prompt tokens, cached prompt tokens, completion tokens, total tokens, estimated USD cost, total latency, outcome, HTTP status, and timestamp. Prompts, generated answers, plaintext virtual keys, and real provider credentials are never stored in this table.

Streaming remains unbuffered: the gateway observes a copy of each SSE chunk and writes the row only after the stream completes, fails, or disconnects. Fireworks requests automatically include `stream_options.include_usage=true`, while Anthropic's translated final event already includes normalized token usage.

Until the Phase 5 reporting endpoint or CLI is added, inspect the safe ledger directly:

```bash
sqlite3 auth/gateway.db \
  "SELECT virtual_key_id, provider, model, total_tokens, estimated_cost_usd, cache_status, cost_avoided_usd, latency_ms, status, created_at FROM usage_logs ORDER BY id DESC;"
```

Cost estimates use standard list prices stored beside each model route: DeepSeek V4 Flash (0731) uses Fireworks' $0.22 input, $0.007 cached-input, and $0.66 output rates per million tokens; Claude Sonnet 5 uses Anthropic's $2 input and $10 output rates per million tokens. These are estimates rather than invoices, so update `providers/registry.py` when provider prices or serving tiers change.

## Phase 5.1 latency overhead benchmark

Add `PROXY_VIRTUAL_KEY` to your ignored `.env`, start the gateway, and run paired direct-provider and gateway requests:

```bash
python -m benchmarks.latency_overhead \
  --requests 30 \
  --warmups 3 \
  --output benchmarks/results/latency.json
```

The script alternates request order, reuses separate HTTP connections, and prints direct, gateway, and gateway-minus-direct p50/p95/p99 latency. It deliberately uses `temperature: 0.2`, so Phase 5.2 caching cannot make gateway measurements look artificially fast.

Do not describe a synthetic run as production evidence. Run this command against the real provider and the same gateway deployment you are describing on your resume.

## Phase 5.2 response caching

Caching is available only for non-streaming requests when either:

- `temperature` is explicitly `0`, unless `"cache": false` opts out; or
- the application explicitly sends `"cache": true`.

The `cache` field belongs to ProxyLLM and is removed before forwarding. Entries are stored in SQLite for one hour and scoped to the virtual key, normalized model, prompt, and parameters, preventing responses from crossing application boundaries.

Responses expose measurement headers:

```text
X-Proxy-Cache: HIT | MISS | BYPASS | ERROR
X-Proxy-Estimated-Cost-USD: provider estimate for a miss
X-Proxy-Cost-Avoided-USD: estimated provider cost avoided by a hit
```

`usage_logs.cache_status` records `hit`, `miss`, or `not_eligible`, and `usage_logs.cost_avoided_usd` records the estimated savings. Run a fresh repeated-query workload with:

```bash
python -m benchmarks.cache_workload \
  --unique-prompts 10 \
  --repeats 5 \
  --output benchmarks/results/cache.json
```

The script adds a unique run namespace so the first round is a real miss, then reports cache hit rate and estimated provider spend avoided together.

## Phase 5.3 load test

Install k6 on macOS and verify it:

```bash
brew install k6
k6 version
```

Start with modest levels because real-provider load tests spend money, then sweep complete and streaming calls independently:

```bash
python -m benchmarks.load_sweep \
  --levels 10,25,50 \
  --stage-duration 10s \
  --p95-limit-ms 2000 \
  --error-rate-limit 0.01 \
  --mode both \
  --output benchmarks/results/load-sweep.json
```

The wrapper runs `benchmarks/load_test.js` once per concurrency level and response mode, then reports the highest level actually tested where p95 and error rate both stayed below your thresholds. Caching is explicitly disabled during load tests, and the current project has no provider fallback, so fallback outage comparison is correctly marked as not applicable.

## Cost-free tooling check with the mock provider

The synthetic provider verifies commands without using provider credit; its results are development evidence, not resume numbers:

```bash
# Terminal 1
uvicorn benchmarks.mock_provider:app --port 9001

# Terminal 2: exported values override .env without displaying it
FIREWORK_URL=http://127.0.0.1:9001/v1/chat/completions \
FIREWORK_API_KEY=mock-secret \
uvicorn api.main:app --port 8000

# Terminal 3
FIREWORK_URL=http://127.0.0.1:9001/v1/chat/completions \
FIREWORK_API_KEY=mock-secret \
python -m benchmarks.latency_overhead --requests 20
```

Set `MOCK_PROVIDER_DELAY_MS` to control provider delay or `MOCK_PROVIDER_FAIL_EVERY` to return a deterministic 500 on every Nth request.
