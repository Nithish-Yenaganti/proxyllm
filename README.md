# ProxyLLM

ProxyLLM is a local OpenAI-compatible gateway with virtual-key authentication, SQLite provider permissions, streaming responses, model-based routing for Fireworks and Anthropic, and per-request usage logging.

## Configure providers

Copy the variable names from `.env.example` into your ignored `.env` and fill in only the credentials you intend to use.

```text
FIREWORK_API_KEY=...
FIREWORK_URL=https://api.fireworks.ai/inference/v1/chat/completions
ANTHROPIC_API_KEY=...
ANTHROPIC_URL=https://api.anthropic.com/v1/messages
```

Never commit or display `.env`.

## Initialize or migrate SQLite

```bash
python -m auth.database
```

Existing Phase 2 keys automatically keep their original provider permission.

The same startup migration also creates the Phase 5.1 `usage_logs` table.

## Manage virtual keys and permissions

```bash
# Create a key with its first Fireworks permission.
python -m auth.cli create --app jan --provider fireworks

# Safely list key IDs, prefixes, status, and provider permissions.
python -m auth.cli list

# Grant that same key access to Anthropic.
python -m auth.cli grant --id 1 --provider anthropic --credential default

# Revoke the complete key and all of its provider access.
python -m auth.cli revoke --id 1
```

The complete virtual key is displayed only once when it is created.

## Run the gateway

```bash
uvicorn api.main:app --reload
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

## Phase 5.1 usage logging

Every authenticated chat request writes one row containing the virtual-key ID, provider, public model, prompt tokens, cached prompt tokens, completion tokens, total tokens, estimated USD cost, total latency, outcome, HTTP status, and timestamp. Prompts, generated answers, plaintext virtual keys, and real provider credentials are never stored in this table.

Streaming remains unbuffered: the gateway observes a copy of each SSE chunk and writes the row only after the stream completes, fails, or disconnects. Fireworks requests automatically include `stream_options.include_usage=true`, while Anthropic's translated final event already includes normalized token usage.

Until the Phase 5 reporting endpoint or CLI is added, inspect the safe ledger directly:

```bash
sqlite3 auth/gateway.db \
  "SELECT virtual_key_id, provider, model, total_tokens, estimated_cost_usd, latency_ms, status, created_at FROM usage_logs ORDER BY id DESC;"
```

Cost estimates use standard list prices stored beside each model route: DeepSeek V4 Flash (0731) uses Fireworks' $0.22 input, $0.007 cached-input, and $0.66 output rates per million tokens; Claude Sonnet 5 uses Anthropic's $2 input and $10 output rates per million tokens. These are estimates rather than invoices, so update `providers/registry.py` when provider prices or serving tiers change.
