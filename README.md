# ProxyLLM

ProxyLLM is a local OpenAI-compatible gateway with virtual-key authentication, SQLite provider permissions, streaming responses, and model-based routing for Fireworks and Anthropic.

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
