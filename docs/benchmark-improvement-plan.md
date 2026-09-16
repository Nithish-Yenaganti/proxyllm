# Benchmark evidence plan

Goal: make measurements repeatable and honest, not make numbers look bigger.

| Phase | Change | Validation | Rollback |
|---|---|---|---|
| 1 | Record checkout revision, dirty state, settings and individual timing samples | Unit tests; inspect a saved report for secrets | Revert benchmark-only changes |
| 2 | Run isolated load at 1, 5 and 10 clients, streaming first-content timing, and a mixed cache workload | Free local suite using temporary databases and mock provider | Stop test processes; live data is untouched |
| 3 | Collect larger real-provider paired samples with agreed spending allowance | Repeat on the same configuration; compare distributions and failures | Stop measurements; no application changes |

Phases 1–2 are implemented and locally exercised. A bounded real run completed
ten measured pairs per provider; see [results](real-provider-check-results.md).
Larger paid runs still require approval and are not needed to finish the local demo.

## Free local run

```sh
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m client_testing.evidence_suite
```

Each run makes a new ignored folder under `benchmarks/results`; it refuses to overwrite an existing folder.
No real keys, live database, or running gateway are needed.
Each case starts fresh test servers and keys, and uses the current pooled gateway code.

Reports count completed responses separately from 429 rate rejections, 503 busy responses,
transport errors and incomplete HTTP-200 streams.
TTFT means time from submission to the first nonempty content delta, not response headers.
Reports keep timings and selected usage headers, not prompts, answers or credentials.

## What the numbers mean

The short suite is a repeatability check, not a maximum-capacity test.
Workers are closed-loop: each waits for its response before sending another request.
The default 25 ms mock delay and tiny answers do not represent a real model.
Cache repeats are synthetic and isolated per key; their hit rate is not a prediction of real savings.
Five provider slots and the per-key rate limit remain enabled; increasing clients does not remove them.
The recorded commit identifies the checkout, and `dirty: true` means uncommitted changes were present.

For longer experiments, use `client_testing.http_load --help` to set duration, interval,
mock delay, streaming or cache mix; count rejected requests separately.
For real latency runs, keep model, output allowance and cache behavior equal, pace requests
below the key limit, alternate direct/gateway order, and collect multiple runs before claiming a gain.
Small-sample p95/p99 values and negative apparent overhead are not proof the proxy accelerates the provider.
Larger real-provider samples, memory profiling and a sustained capacity ceiling
remain optional follow-up work; the completed small run does not establish those.
