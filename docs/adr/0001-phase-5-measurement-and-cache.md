# ADR 0001: Phase 5 Measurement and Response Cache Architecture

## Status

Accepted and implemented for the current single-host ProxyLLM project.

## Decision

Keep ProxyLLM as one FastAPI service and keep authentication, usage logs, and response caching in SQLite. Use standalone Python benchmarks, a k6 load script, and a local mock provider rather than adding another production service.

## Recommended stack

- FastAPI and HTTPX remain the HTTP boundary.
- SQLite through aiosqlite stores cache entries and cache measurements.
- Python scripts calculate paired latency percentiles and cache workload savings.
- k6 supplies controlled concurrent streaming and non-streaming load.

## Data model direction

`usage_logs` gains `cache_status` and `cost_avoided_usd`. `response_cache` stores a
SHA-256 key, owning virtual-key ID, provider/model identity, complete successful
response, safe headers, original estimated cost, and one-hour reuse deadline.

The hash covers the virtual-key ID and canonical JSON for model, messages, and request parameters after removing gateway-only cache control. Including the virtual-key ID intentionally prevents cross-application response leakage.

Updated implementation note: the Anthropic adapter rejects untranslated settings,
including `temperature`, before cache lookup. Use `cache: true` for explicit reuse
or `cache: false` for a fresh call. Caching does not guarantee deterministic sampling.

## System boundaries

- Authentication, authorization, and provider configuration must succeed before cache lookup.
- `usage/cache.py` owns cache policy and key normalization.
- `auth/database.py` owns SQLite schema and persistence until a broader storage package is justified.
- `api/main.py` coordinates hits, misses, provider calls, response headers, and usage logs.
- `benchmarks/` measures the running system but is never imported by production request handling.

## Integrations

The latency and cache scripts use the gateway's existing OpenAI-compatible HTTP endpoint. k6 uses the same endpoint and virtual-key header as real applications, while the optional mock provider implements only the small Fireworks-compatible surface required for safe local validation.

## Rejected alternatives

- Redis was rejected because the current project is a single local process and does not need another service to operate.
- In-memory caching was rejected because restart behavior and multiple Uvicorn workers would make results inconsistent.
- Caching streaming output was rejected because replaying a buffered answer would not behave like provider streaming and would corrupt latency claims.
- Global cross-app cache entries were rejected because identical hashes could leak one application's response to another.
- Average-only benchmarks were rejected because they hide tail latency.

## Reason for the choice

This design reuses the existing deployable components, produces inspectable measurements, and remains reversible. The cache can later move behind the same small database function boundary without changing the public API.

## Operational cost

SQLite needs no separate server, but every eligible request adds one cache lookup and
successful cacheable responses add one write. Expiry prevents reuse but does not delete
a row, so cache bodies continue consuming local disk until an operator or future
maintenance command removes expired entries.

Real latency, cache, and load workloads consume provider credit. The mock provider verifies mechanics at zero provider cost, but synthetic numbers must never be presented as production measurements.

## Scaling limit

SQLite WAL mode supports this project's single-host concurrent readers and short writes, but write contention and local-disk ownership become limiting with multiple gateway replicas or sustained high request volume. At that point, migrate response caching to Redis and usage logs to a server database or asynchronous event pipeline.

## Migration risk

The migration adds columns with defaults, so existing usage rows remain valid. Cache entries are disposable derived data, making rollback low-risk; deleting the cache table loses only reusable responses, not keys or usage history.

## What would be overkill

Redis Cluster, Kafka, distributed tracing infrastructure, and a separate benchmark service would increase maintenance without improving the current learning milestone.

## What would be dangerous to skip

- Per-key cache scoping and authorization before lookup.
- A TTL for stale generated responses.
- Explicit cache hit/miss and avoided-cost logging.
- Tail percentiles and error rates during performance tests.
- Warmups, raw samples, fixed workloads, and saved configuration for reproducibility.
- Clear separation between synthetic mock results and real measured provider results.
