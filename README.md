# ProxyLLM

The goal is to let apps use different model providers without each app managing
separate provider credentials and access rules.

ProxyLLM lets an app call Fireworks or Anthropic through one OpenAI-compatible API.
The app uses a virtual key; the proxy checks its permissions and keeps the real
provider keys on the server.

Built with Python, FastAPI and SQLite. Tested locally with both providers;
not deployed or ready for public use.

## What it does

- Routes chat and streaming requests, reusing provider connections.
- Limits each key to 24 requests per rolling minute and each request to 5 MB by default.
- Allows five active provider calls per process, with a two-second wait for a slot.
- Caches opted-in non-streaming responses for 30 minutes, separately for each key.
- Shows usage and estimated costs in a CLI and read-only local dashboard.
- Includes request inspection, database backups and manual cache cleanup.

## Quick start

Use Python 3.10+ on macOS/Linux. Run these commands from the project directory.

### 1. Install and configure

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Set `FIREWORK_API_KEY` and `FIREWORK_URL` in `.env`; the example includes the URL.
For Anthropic, also set `ANTHROPIC_API_KEY`. Keep this file out of Git.

### 2. Create a virtual key

```bash
python -m auth.database
python -m auth.cli create --app my-app --provider fireworks
```

Save the key shown once by the command. To give it Anthropic access too, replace
`1` below with its actual ID:

```bash
python -m auth.cli list
python -m auth.cli grant --id 1 --provider anthropic --credential default
```

### 3. Start the gateway

```bash
python -m uvicorn api.main:app --reload
```

Open `http://127.0.0.1:8000` to check it is running. Stop with Control-C.
`--reload` is for development only; restart after configuration changes.

### 4. Connect your app

For an OpenAI-compatible client, use:

| Setting | Value |
|---|---|
| API base URL | `http://127.0.0.1:8000/v1` |
| API key | Your virtual key, not a provider key |
| Fireworks model | `fireworks/deepseek-v4-flash` |
| Anthropic model | `anthropic/claude-sonnet-5` |

The key needs permission for the selected provider. Real-provider calls cost money.
You can also use the [Python demo](docs/demo.md) instead of a separate app.

## Important behavior

- Anthropic supports text chat, not every OpenAI feature. Unsupported settings are
  rejected unless an administrator explicitly permits sampling-field removal;
  see [parameter policy](docs/parameter-policy.md). No temperature default is added.
- Anthropic uses a 2,048-token output allowance when the client supplies none.
  Answers can still reach their token limit.
- `cache: true` enables caching; `cache: false` disables it. Otherwise, effective
  temperature zero enables it. Streaming is never cached, and expiry does not
  guarantee that an answer is current.
- Usage logs omit prompts and answers, but cached responses and backups contain
  answers. Keep keys, environment files, databases and backups private.
- The five-call limit is per process, not shared across workers. Cache hits and
  inspection still count toward the per-key rate limit.

## Usage and tests

```bash
python -m auth.cli usage
python -m unittest discover -s tests -v
```

The [demo](docs/demo.md) checks both providers, permissions, streaming and cache
isolation. See [saved results](docs/real-provider-check-results.md) for measurements
and caveats: the samples are small, and some benchmark answers hit the token limit.
They do not establish production capacity.

Usage costs are estimates, not invoices; middleware rejections and failed usage
writes are excluded. The [dashboard](dashboard/README.md) is localhost-only.
More [testing instructions](client_testing/README.md) and the legacy k6 report
limitation are documented in [decision history](DECISIONS.md).

## Backup and cleanup

```bash
# Create and verify a protected backup before maintenance.
python -m auth.backup create

# Preview only: no deletion.
python -m auth.cache_cleanup

# Explicitly back up first, then delete expired cache rows.
python -m auth.cache_cleanup --delete
```

Backups are not currently scheduled. Same-disk backups cannot protect against
disk loss. See [backup](docs/backups.md) and [cleanup](docs/cache-cleanup.md) guides
for retention rules and recovery steps. Database files and local reports stay out of Git.

## Documentation

- [Architecture](ARCHITECTURE.md): components and request flow.
- [Code guide](CODE_GUIDE.md): file-by-file explanations.
- [Decision history](DECISIONS.md): choices, fixes, measurements, and gaps.
- [Request inspection](docs/inspection.md): preview a request without a provider call.
- [Future work](docs/future-work.md): optional deployment and operational improvements.
- [Deployment guide](docs/deployment-guide.md): local steps and remaining deployment work.
- [Learning journal](docs/learning-journal.md) and [failure log](docs/failure-log.md): actual exercises and results.
