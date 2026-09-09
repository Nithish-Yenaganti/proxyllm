# Python client testing

## Sustained local HTTP smoke load

```bash
python -m client_testing.http_load --pooled --clients 8 --seconds 30 --output benchmarks/results/local-http-pooled.json
```

Creates two separate Uvicorn subprocesses on temporary localhost ports: a mock
provider and an isolated proxy wired to a temporary SQLite database. Real server
configuration is replaced only inside the test subprocess. Fresh temporary keys
send complete, uncached requests at one request per key every three seconds;
the normal limiter stays enabled. Both test subprocesses stop and temporary data
is removed when the run ends. Your regular proxy is not stopped or modified.

Use `--pooled` to match normal server connection reuse; omit it only to compare
with the older per-request client behavior.

The selected output report records HTTP status counts,
successful responses per second, and successful latency percentiles. Repeat runs
overwrite that report unless you supply `--output`. This is a paced stability
check, not a saturation benchmark: offered traffic is approximately clients/3
requests per second, with synchronized client bursts. It includes localhost HTTP
and Uvicorn but excludes TLS, real provider delays, and streaming. No production
capacity or sustained maximum can be inferred from a successful run.

## Free isolated load experiment

```bash
python -m client_testing.load_test
```

Runs complete-response batches with 1, 4, and 8 concurrent clients against an
in-process mock provider. Every level gets a temporary SQLite database and fresh
keys; each key sends only 10 requests, keeping the real 24-request limiter enabled.
No sockets or real provider calls are made, and the running server and its data
are untouched. Temporary databases are removed afterward. The JSON report is saved
to `benchmarks/results/mock-load.json` (overwritten on another run).

Successful throughput counts only HTTP 200 responses; other status counts are
reported separately. Latency percentiles cover successful calls. This measures
bounded in-process application work, NOT sustained network/server capacity or
real-provider performance. It excludes Uvicorn, TCP, TLS, and streaming behavior.
Use `--levels 1 4 8 --requests-per-client 10 --output PATH` to customize the run.

## Checks against your running proxy

Run the following checks from the repository root with the virtual environment activated and
the proxy running. These scripts do not start the server or issue keys.
Set `PROXY_VIRTUAL_KEY` in your ignored `client_testing/.env`; for isolation set a different
`PROXY_SECOND_VIRTUAL_KEY`. Never commit credentials. Use only a trusted proxy
address; use HTTPS when connecting beyond localhost.

The scripts load `client_testing/.env` explicitly; exported shell variables take
precedence. `.env.example` contains the expected names. Keep real provider keys
in the root server `.env`, not this client file. The benchmark wrappers also load
the root environment through their existing benchmark entry points (the direct
Fireworks latency comparison needs the real provider key).

Replace `YOUR_MODEL` with a public model name configured in the registry.
Both keys must have permission for that model's provider for isolation testing.
Run chat and stream once for each provider you intend to verify.

```bash
python -m client_testing.check_client chat --model YOUR_MODEL
python -m client_testing.check_client stream --model YOUR_MODEL
python -m client_testing.check_client cache --model YOUR_MODEL
python -m client_testing.check_client isolation --model YOUR_MODEL
```

These checks can incur real provider costs and write usage/cache records. Chat
and stream each make one call; cache normally makes one; isolation normally makes
two. Each run uses a unique harmless prompt. Outputs omit generated text and keys.
These are bounded smoke checks, not complete compatibility or security tests.

For permission denial, use a key without access to the selected provider:

```bash
python -m client_testing.check_client denied --model YOUR_MODEL
```

For rate limiting, use a fresh key or one idle for at least 60 seconds. This sends
25 invalid requests: 24 should return 400 and the last 429. No provider is called.
Run it separately from other checks; it consumes the key's admission allowance.

```bash
python -m client_testing.check_client rate-limit --model unused
python -m auth.cli usage
```

## Saved benchmarks

The wrappers reuse existing benchmarks rather than copying measurement logic.
Inspect `--help` for model, URL, and credential options. The latency benchmark
requires a real Fireworks key for its direct calls as well as the proxy key.

```bash
python -m client_testing.latency --requests 5 --warmups 1 --output benchmarks/results/client-latency.json
python -m client_testing.cache_benchmark --unique-prompts 2 --repeats 2 --output benchmarks/results/client-cache.json
```

Start with these small workloads and an idle key. Large/default cache workloads
can exceed 24 requests in 60 seconds; throttled runs are not clean cache or
capacity measurements. Use the existing mock provider for unpaid experiments.
These wrappers measure latency and caching, not maximum sustainable throughput.
Do not claim benchmark results until a run succeeds and its report is inspected.
