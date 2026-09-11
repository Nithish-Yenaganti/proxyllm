# ProxyLLM

One OpenAI-compatible endpoint for Fireworks and Anthropic. Apps use virtual keys;
real provider credentials stay on the gateway server.

This is a working local project being prepared for private deployment—not a
production-ready public service.

## What it does

- Checks virtual keys and per-key provider permissions.
- Routes chat requests and streams OpenAI-style responses.
- Reuses provider HTTP connections.
- Limits each key to 24 authenticated requests per rolling 60 seconds.
- Caps incoming request bodies at 5 MB by default.
- Allows five active provider calls per process, waiting up to two seconds for a slot.
- Optionally caches complete responses for 30 minutes, separately for each key.
- Reports usage and estimated costs through a CLI and local dashboard.
- Previews provider requests without making paid calls.
- Creates verified SQLite backups and supports manual expired-cache cleanup.

## Quick start

Use Python 3.10+; backup tooling targets macOS/Linux.
Run commands from the project directory.

### 1. Install and configure

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Edit `.env` with the provider credentials you need. For Fireworks, set
`FIREWORK_API_KEY` and `FIREWORK_URL`; the example file contains the endpoint.
Keep real keys out of client apps and Git. Restart after configuration changes.

### 2. Create a virtual key

```bash
python -m auth.database
python -m auth.cli create --app my-app --provider fireworks
```

Save the virtual key displayed once by the command. To allow Anthropic too,
configure `ANTHROPIC_API_KEY` and grant access using your actual key ID:

```bash
python -m auth.cli list
python -m auth.cli grant --id 1 --provider anthropic --credential default
```

### 3. Start the gateway

```bash
python -m uvicorn api.main:app --reload
```

Local address: `http://127.0.0.1:8000`. Stop with Control-C.
Reload is for development, not deployment.

### 4. Connect your app

For an OpenAI-compatible client, use:

| Setting | Value |
|---|---|
| API base URL | `http://127.0.0.1:8000/v1` |
| API key | Your virtual key, not a provider key |
| Fireworks model | `fireworks/deepseek-v4-flash` |
| Anthropic model | `anthropic/claude-sonnet-5` |

Your key must have permission for the selected provider. See
[client testing](client_testing/README.md) for Python checks and request examples.
Real-provider calls may cost money.

## Important behavior

- **Settings:** Unsupported Anthropic settings are rejected by default.
  Administrators can explicitly allow sampling-field removal by provider or model;
  no default temperature is inserted. See [parameter policy](docs/parameter-policy.md).
- **Compatibility:** Anthropic supports the adapter's text-chat features, not every
  OpenAI feature. Unsupported tools and non-text content are rejected.
- **Cache:** Non-streaming only. `cache: true` opts in; `cache: false` opts out.
  Otherwise, effective temperature zero enables it. A removed temperature cannot
  enable caching. Expiry stops reuse; it does not prove an answer is still correct.
- **Privacy:** Usage logs omit prompts and answers, but response-cache rows and
  database backups contain answers. Keep databases and backups private.
- **Limits:** Cache hits and inspection count toward the rate limit. Excess requests
  return 429; oversized bodies return 413. Configure the body cap through
  `PROXY_MAX_REQUEST_BYTES` (default 5,000,000 bytes).
- **Inspection:** `/v1/inspect` previews preparation without contacting a provider.
  It does not prove upstream acceptance. See [inspection](docs/inspection.md).

## Usage and tests

```bash
python -m auth.cli usage
python -m unittest discover -s tests -v
```

Costs are estimates, not invoices. Middleware rejections and failed usage writes
are not included in the normal usage report. The [local dashboard](dashboard/README.md)
provides a read-only browser view; keep it localhost-only.

Use [client-testing instructions](client_testing/README.md) for checks and
benchmarks. Start with isolated mocks before paid runs. Small samples and mock
workloads are not production-capacity evidence; the legacy k6 sweep also has a
known report-freshness risk recorded in [project history](DECISIONS.md).

## Backup and cleanup

```bash
# Create and verify a protected backup before maintenance.
python -m auth.backup create

# Preview only: no deletion.
python -m auth.cache_cleanup

# Explicitly back up first, then delete expired cache rows.
python -m auth.cache_cleanup --delete
```

Hourly retention keeps three recent snapshots plus one prior-day recovery point.
Maintenance and older unclassified backups stay protected. Hourly execution is
configured through the maintainer's local task automation—it is **not installed
by cloning this repository**. Cleanup is manual.

See [backups](docs/backups.md) and [cleanup](docs/cache-cleanup.md) for details.
Same-disk backups do not protect against disk loss. Secrets, databases, backups,
and local benchmark reports are excluded from Git.

## What's next?

Private deployment still needs HTTPS and restricted access, automatic restart,
broader resource/spending controls, monitoring, and tested operational recovery.
The concurrency limit is per process: use one worker for a five-call total.
Busy requests receive 503 with retry guidance; cache hits and inspection use no
provider slot. Long streams hold a slot until completion or disconnect.
See [future work](docs/future-work.md) for the bounded finishing scope.

## Documentation

- [Architecture](ARCHITECTURE.md): components and request flow.
- [Code guide](CODE_GUIDE.md): file-by-file explanations.
- [Decision history](DECISIONS.md): choices, fixes, measurements, and gaps.
- [Deployment guide](docs/deployment-guide.md): local steps and remaining deployment work.
- [Learning journal](docs/learning-journal.md) and [failure log](docs/failure-log.md): actual exercises and results.
