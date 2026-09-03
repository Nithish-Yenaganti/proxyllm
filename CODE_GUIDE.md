# ProxyLLM Code Guide

This guide explains what each project file does, why it exists, and what its functions or classes are responsible for.

## How the project works

There are five main flows.

### Creating a virtual key

```text
Administrator runs auth CLI
        ↓
Generate a secret virtual key
        ↓
Hash the complete key with SHA-256
        ↓
Store only the hash and safe metadata in SQLite
        ↓
Display the complete key once for the application
```

### Sending an AI request

```text
Jan or another client application
        ↓
Authorization: Bearer <virtual-key>
        ↓
FastAPI authentication middleware
        ↓
Hash the presented key and check SQLite
        ↓
Read the requested model and select its provider adapter
        ↓
Confirm that the virtual key may use that provider
        ↓
Resolve the permitted real provider credential
        ↓
Translate and send through Fireworks or Anthropic
        ↓
Return one OpenAI-compatible response to the client
```

### Routing a model

```text
Requested model
        ↓
Explicit MODEL_ROUTES lookup
        ↓
Provider permission lookup for this virtual key
        ↓
Trusted credential lookup
        ↓
FireworksAdapter or AnthropicAdapter
```

The client selects a model, not an API key or arbitrary provider URL. The model registry controls the real provider and upstream model, while SQLite controls whether this particular virtual key is allowed to use that provider.

### Streaming an AI response

```text
Client sends the normal request with "stream": true
        ↓
Authentication and provider authorization run normally
        ↓
Selected adapter opens its provider with HTTPX streaming enabled
        ↓
Provider sends one Server-Sent Event chunk
        ↓
Fireworks bytes pass through unchanged, while Anthropic events are translated
        ↓
Repeat until data: [DONE] or the client disconnects
        ↓
Always close the Fireworks response and HTTPX client
```

The Fireworks adapter does not parse, join, or rebuild its already-compatible SSE events. The Anthropic adapter must parse named Anthropic events and emit equivalent OpenAI-compatible chunks because the two streaming schemas differ.

### Recording request usage

```text
Authenticated chat request enters the route
        ↓
Start a monotonic latency timer
        ↓
Route and call the selected provider normally
        ↓
Read usage from complete JSON or observe SSE chunks without buffering
        ↓
Calculate cost from the selected model route's price snapshot
        ↓
Insert one safe usage_logs row when the response finishes
```

Validation failures, authorization denials, configuration failures, provider errors, truncated streams, and client disconnects receive separate status values. The logger stores metrics and identifiers but never stores prompts, answers, plaintext virtual keys, or provider secrets.

---

## `api/main.py`

### Purpose

This is the main FastAPI application. Uvicorn imports it to start the HTTP server.

### Why we need it

It connects the HTTP API, authentication middleware, model routing, provider permissions, credential lookup, and adapter interface.

### Important values

#### `PROVIDER_CREDENTIALS`

Maps a safe provider reference from SQLite to real provider configuration loaded from `.env`.

Example mapping:

```text
fireworks + default
        ↓
FIREWORK_API_KEY + FIREWORK_URL

anthropic + default
        ↓
ANTHROPIC_API_KEY + ANTHROPIC_URL
```

The client never receives or directly uses either real provider key.

### Functions

#### `gateway_error(status_code, message, error_type, code)`

Creates one consistent OpenAI-compatible JSON error envelope for validation, authorization, configuration, and provider-connection failures.

#### `lifespan(_app)`

Runs when FastAPI starts.

It calls `initialize_database()` to create or migrate both SQLite tables before requests arrive.

#### `read_root()`

Handles:

```http
GET /
```

It returns a simple health response:

```json
{"message": "Hello, World!"}
```

This route is public so you can check whether the server is running.

#### `chat_completions(request)`

Handles:

```http
POST /v1/chat/completions
```

It:

1. Reads the authenticated virtual-key metadata added by middleware.
2. Validates the incoming JSON object and required `model` field.
3. Finds the model in the explicit routing registry.
4. Checks whether this key may use the routed provider.
5. Resolves the permitted server-side credential from `.env`.
6. Creates a provider-independent `AdapterRequest`.
7. Calls the registered Fireworks or Anthropic adapter.
8. Returns a complete `Response` or lazy `StreamingResponse`.

It returns:

- `400` when the JSON or provider translation is invalid.
- `403` when the virtual key is not allowed to use the routed provider.
- `404` when the requested model is not registered.
- `500` when the server is missing provider configuration.
- `502` when the selected provider cannot complete the request.

---

## `api/middleware.py`

### Purpose

Authenticates every request whose path starts with `/v1/`.

### Why we need it

Without middleware, any application could use the proxy and spend the real provider account's credit. Middleware makes applications authenticate to our proxy using virtual keys.

### Functions and classes

#### `unauthorized_response()`

Creates the standard `401 Unauthorized` response used for all authentication failures.

It deliberately gives the same response for missing, invalid, unknown, and revoked keys so callers cannot discover which keys previously existed.

#### `VirtualKeyAuthMiddleware`

The class that runs before protected route handlers.

##### `__init__(app, database_path)`

Stores the application and SQLite database path.

The configurable path lets automated tests use temporary databases instead of the real `gateway.db`.

##### `dispatch(request, call_next)`

Runs for every incoming HTTP request.

It:

1. Allows public routes such as `GET /` to continue.
2. Reads the `Authorization` header for `/v1/*` routes.
3. Requires the `Bearer` authentication scheme.
4. Checks the public virtual-key format.
5. Hashes the presented key.
6. Looks for an active matching hash in SQLite.
7. Returns `401` when authentication fails.
8. Adds safe key metadata to `request.state.virtual_key` when authentication succeeds.
9. Calls the protected route.

---

## `api/__init__.py`

### Purpose

Marks `api` as a Python package.

### Why we need it

It lets Python use imports such as:

```python
from api.middleware import VirtualKeyAuthMiddleware
```

---

## `providers/base.py`

### Purpose

Defines the common provider adapter interface and the data passed across that boundary.

### Important types and helpers

- `ProviderCredential` holds one already-authorized provider URL and real key.
- `AdapterRequest` holds the unified body, public model, upstream model, credential, and disconnect callback.
- `AdapterResponse` holds status, safe headers, and either complete bytes or an asynchronous byte iterator.
- `ProviderAdapter` requires every provider implementation to expose `send(request)`.
- `ProviderRequestError` represents a client request that cannot be translated safely.
- `ProviderConnectionError` represents upstream connection or protocol failure.
- `create_http_client()` creates the production HTTPX client.
- `get_response_headers()` copies only safe meaningful provider headers.
- `forward_raw_stream()` passes compatible SSE bytes through and always closes upstream resources.

---

## `providers/fireworks.py`

### Purpose

Implements `FireworksAdapter`, the OpenAI-compatible provider adapter.

It copies the unified request, replaces the public model alias with the trusted upstream model, applies the real Fireworks Bearer key, and preserves both complete JSON and raw SSE behavior. For streaming calls it adds `stream_options.include_usage=true` so the provider emits final token totals required by Phase 5.1.

---

## `providers/anthropic.py`

### Purpose

Implements `AnthropicAdapter` and the translation between OpenAI chat completions and Anthropic Messages.

### Main translations

- `system` and `developer` messages become Anthropic's top-level `system` field.
- User and assistant text remain conversation messages.
- `max_tokens` and `stop` become Anthropic Messages parameters.
- Anthropic content blocks become one OpenAI assistant message.
- Anthropic usage and stop reasons become OpenAI-compatible fields.
- Anthropic named streaming events become OpenAI `chat.completion.chunk` events.
- The adapter adds `data: [DONE]` because Anthropic does not send that OpenAI marker.

The current Anthropic adapter deliberately supports text chat only. It rejects tool calls and non-text content instead of silently discarding information.

---

## `providers/registry.py`

### Purpose

Contains explicit public-model routes, their standard per-million-token prices, and the registered adapter instances.

`get_model_route(model)` returns the provider and trusted upstream model for one exact public name. `get_provider_adapter(provider)` returns the concrete implementation without putting provider-specific conditionals in `main.py`.

---

## `providers/__init__.py`

Marks `providers` as a Python package.

---

## `auth/keys.py`

### Purpose

Contains virtual-key generation, hashing, format validation, and safe display helpers.

### Why we need it

Keeping these operations in one module ensures the CLI and middleware use exactly the same key rules.

### Constants

#### `KEY_PREFIX`

The public marker at the beginning of each key:

```text
nk_
```

#### `KEY_SUFFIX`

The public marker at the end of each key:

```text
_y
```

#### `KEY_RANDOM_BYTES`

Controls how much secure randomness is used. It is currently `32` bytes, or 256 bits.

### Functions

#### `generate_virtual_key()`

Uses Python's `secrets` module to generate an unpredictable authentication key.

We use `secrets`, not `random`, because authentication credentials must not be predictable.

#### `hash_virtual_key(virtual_key)`

Converts the text key into UTF-8 bytes and hashes it with SHA-256.

The same key always produces the same hash. SQLite stores this hash instead of the plaintext key.

#### `has_valid_key_format(virtual_key)`

Checks that a presented key has the expected prefix, suffix, and non-empty random part.

This quickly rejects obviously malformed values before querying SQLite.

It does not prove that the key is authentic; the database lookup performs that check.

#### `get_key_prefix(virtual_key)`

Returns only the first 12 characters of a key.

This short value helps administrators recognize a record without exposing enough information to authenticate.

---

## `auth/database.py`

### Purpose

Creates the SQLite schema and contains database operations for virtual keys, provider permissions, and the Phase 5.1 usage ledger.

### Why we need it

It gives the CLI and authentication middleware one shared persistence layer and prevents SQL logic from spreading across unrelated files.

### Important values

#### `DATABASE_PATH`

Points to the ignored runtime database:

```text
auth/gateway.db
```

#### `CREATE_VIRTUAL_KEYS_TABLE`

Defines the `virtual_keys` table.

#### `CREATE_PROVIDER_PERMISSIONS_TABLE`

Defines `virtual_key_provider_permissions`, where each row grants one key access to one provider and named server-side credential.

#### `BACKFILL_PROVIDER_PERMISSIONS`

Copies every existing Phase 2 key's original provider fields into the new permission table. `INSERT OR IGNORE` makes this migration safe to run repeatedly.

#### `CREATE_USAGE_LOGS_TABLE`

Defines the append-only `usage_logs` accounting table linked to the virtual key that made each authenticated request.

### Table fields

| Field | Meaning |
|---|---|
| `id` | Numeric identifier used by the revoke command |
| `app_name` | Application that owns the virtual key |
| `key_prefix` | Short safe key identifier |
| `key_hash` | SHA-256 hash used for authentication |
| `provider` | Provider the key may use |
| `provider_credential` | Named real credential the server may resolve |
| `is_active` | `1` for active or `0` for revoked |
| `created_at` | Time the key was created |
| `revoked_at` | Time the key was revoked, when applicable |

The original `provider` columns remain for backward compatibility and migration history. Phase 4 authorization uses the separate permission table.

### Provider-permission fields

| Field | Meaning |
|---|---|
| `key_id` | Virtual key receiving the permission |
| `provider` | Routed provider this key may use |
| `provider_credential` | Named server-side credential the provider may resolve |

### Usage-log fields

| Field | Meaning |
|---|---|
| `virtual_key_id` | Safe numeric owner of the request |
| `provider` | Routed provider, or null when routing could not finish |
| `model` | Client-visible requested model |
| `prompt_tokens` | Total normalized input tokens |
| `cached_prompt_tokens` | Cached subset reported by the provider |
| `completion_tokens` | Generated output tokens |
| `total_tokens` | Combined input and output tokens |
| `estimated_cost_usd` | Cost estimate calculated from that model route |
| `latency_ms` | Route time through complete response or stream termination |
| `status` | Searchable outcome such as `success`, `denied`, or `provider_error` |
| `status_code` | HTTP status returned to the application |
| `created_at` | SQLite timestamp for later daily reporting |

### Functions

#### `initialize_database(database_path)`

Creates all SQLite tables and indexes and backfills old permissions when they do not exist.

It is safe to run repeatedly because the SQL uses `CREATE TABLE IF NOT EXISTS`.

#### `create_virtual_key_record(...)`

Inserts one key's safe metadata and hash together with its first provider permission.

It never receives or stores the plaintext virtual key. It returns the new numeric record ID.

#### `list_virtual_key_records(database_path)`

Returns safe metadata for active and revoked records, including each key's provider-permission list.

It deliberately excludes `key_hash`, so the CLI cannot accidentally display stored authentication data.

#### `get_active_virtual_key_by_hash(key_hash, database_path)`

Finds a record only when its hash matches and `is_active` is `1`.

The middleware uses this function to authenticate requests. Revoked and unknown keys both return `None`.

#### `get_provider_permission_for_key(record_id, provider, database_path)`

Returns the named credential only when that key is active and has an explicit permission for the provider selected by model routing.

#### `create_usage_log_record(...)`

Appends one completed request's safe metrics to `usage_logs` and returns its numeric log ID.

#### `list_usage_log_records(database_path)`

Returns usage rows in insertion order for isolated tests and the later reporting feature.

#### `grant_provider_permission(record_id, provider, provider_credential, database_path)`

Adds or updates one provider permission for an active virtual key. It returns `False` for unknown or revoked keys.

#### `revoke_virtual_key_record(record_id, database_path)`

Changes an active record to:

```text
is_active = 0
revoked_at = current time
```

It returns `True` when a record was revoked and `False` when the ID was unknown or already revoked.

---

## `auth/cli.py`

### Purpose

Provides trusted local terminal commands for managing virtual keys.

### Why we need it

An HTTP key-management endpoint would require a separate administrator-authentication system. A local CLI is simpler and safer for Phase 2.

### Functions

#### `build_parser()`

Defines these commands and their arguments:

```bash
python -m auth.cli create --app jan
python -m auth.cli grant --id 1 --provider anthropic
python -m auth.cli list
python -m auth.cli revoke --id 1
```

#### `create_key(app_name, provider, credential)`

It:

1. Generates one plaintext key.
2. Hashes the complete key.
3. Creates a short safe prefix.
4. Stores only the hash and metadata.
5. Prints the complete key once.

#### `list_keys()`

Displays IDs, application names, prefixes, provider permissions, and active status.

It never displays complete keys or hashes.

#### `grant_key_permission(record_id, provider, credential)`

Gives one existing active key permission to use another routed provider credential.

#### `revoke_key(record_id)`

Revokes one active key by its numeric database ID.

It does not affect any other application's key.

#### `run_command(arguments)`

Routes parsed CLI arguments to `create_key`, `grant_key_permission`, `list_keys`, or `revoke_key`.

---

## `auth/__init__.py`

### Purpose

Marks `auth` as a Python package.

### Why we need it

It makes imports such as these possible:

```python
from auth.keys import hash_virtual_key
from auth.database import initialize_database
```

---

## `usage/tracking.py`

### Purpose

Extracts normalized token totals, calculates model-specific cost estimates, and observes streamed SSE output without buffering or changing its bytes.

### Main parts

- `TokenUsage` holds prompt, cached prompt, completion, and total counters.
- `extract_token_usage()` validates a decoded OpenAI-compatible usage object.
- `extract_token_usage_from_body()` reads usage from a complete JSON response.
- `estimate_cost_usd()` uses exact decimal arithmetic and per-million-token route prices.
- `OpenAIStreamObserver` incrementally finds usage, error, and `[DONE]` events, including inside gzip or deflate streams.
- `observe_stream()` yields every original chunk immediately and runs one final logging callback after success, failure, cancellation, or disconnect.

## `usage/__init__.py`

Marks Phase 5 usage utilities as an importable Python package.

---

## `tests/test_keys.py`

### Purpose

Tests virtual-key helpers without using the database or HTTP server.

### Tests

- Generated keys are different and correctly formatted.
- The same key produces the same hash.
- Different keys produce different hashes.
- SHA-256 produces 64 hexadecimal characters.
- A display prefix is shorter than the complete key.

---

## `tests/test_database.py`

### Purpose

Tests key storage, migration, multi-provider permissions, lookup, and revocation using temporary SQLite databases.

### Tests

- A hash and its metadata can be inserted and listed.
- Safe listings do not expose `key_hash`.
- Active hashes can be found.
- Existing provider fields become the initial permission automatically.
- One key can receive both Fireworks and Anthropic permissions.
- Ungranted providers remain unavailable.
- Revocation disables all permissions for that key.
- Revoking one key does not revoke another key.

Temporary databases protect real application records from automated tests.

---

## `tests/test_middleware.py`

### Purpose

Tests HTTP authentication using an in-process FastAPI application.

### Tests

- The health route is public.
- Missing keys receive `401`.
- Incorrect authentication schemes receive `401`.
- Unknown keys receive `401`.
- Active keys reach protected routes.
- Revoked keys receive `401` immediately.

---

## `tests/test_streaming.py`

### Purpose

Tests that Phase 3 Fireworks behavior remains correct after moving it behind the Phase 4 adapter.

### Helpers

#### `TrackedByteStream`

Acts like a provider's asynchronous response body and records whether the proxy closed it.

#### `DisconnectState`

Simulates a connected or disconnected client so cleanup behavior can be verified.

### Tests

- SSE chunks are returned in their original order without being rewritten.
- The final `data: [DONE]` event reaches the client.
- `stream: true` and client fields reach the provider, with `include_usage=true` added for accounting.
- The real provider key replaces the virtual key on the outbound request.
- Streaming response headers are preserved without adding `Content-Length`.
- Provider response and HTTPX client resources close after completion.
- A client disconnect stops forwarding and closes upstream resources.
- Requests without `stream: true` retain the complete Phase 2 JSON behavior.

---

## `tests/test_providers.py`

### Purpose

Tests both model adapters and all Anthropic translation without reading `.env` or contacting a real provider.

### Tests

- Explicit model names route to Fireworks or Anthropic.
- Unknown names are not guessed from prefixes.
- OpenAI system and developer messages move to Anthropic's system field.
- Anthropic headers, request fields, text content, finish reasons, and usage are normalized.
- Anthropic named SSE events become ordered OpenAI chunks and `data: [DONE]`.
- Unsupported tool requests fail clearly.
- Anthropic error envelopes become OpenAI-compatible errors.
- Stream resources close after completion.

---

## `tests/test_routing.py`

### Purpose

Tests the real `/v1/chat/completions` route with in-memory credentials, permission lookups, and recording adapters.

### Tests

- The same endpoint selects Fireworks or Anthropic based on `model`.
- The adapter receives the trusted upstream model and server-side credential.
- Unknown models return `404` before provider work.
- Missing provider permissions return `403`.
- Successful, unknown-model, and denied requests invoke the isolated usage writer.

---

## `tests/test_usage.py`

### Purpose

Tests Phase 5.1 accounting without touching the real database or provider network.

### Tests

- Complete JSON usage and cached prompt counters are normalized.
- Input, cached-input, and output model rates produce an exact cost estimate.
- Arbitrarily split SSE chunks remain byte-for-byte unchanged while usage is observed.
- Gzip SSE output remains compressed for the client while its usage copy is decoded.
- A complete usage record survives a round trip through temporary SQLite storage.

---

## `tests/__init__.py`

Marks `tests` as a Python test package.

---

## `.env`

### Purpose

Stores local real-provider configuration such as Fireworks and Anthropic URLs and API keys.

### Security rule

This file is ignored by Git and must never be committed or displayed.

## `.env.example`

Documents the required environment-variable names and safe endpoint examples without containing real credentials.

---

## `.gitignore`

### Purpose

Prevents local secrets, virtual environments, caches, editor files, and SQLite databases from entering Git.

Important protected files include:

```text
.env
.venv/
*.db
*.sqlite
*.sqlite3
```

---

## `requirements.txt`

### Purpose

Records the Python packages and versions needed to recreate the environment.

Important packages include:

- `fastapi` for HTTP routes and middleware.
- `uvicorn` for running the ASGI server.
- `httpx` for outbound provider requests.
- `aiosqlite` for asynchronous SQLite access.
- `python-dotenv` for loading local environment variables.

---

## `smoketest.py`

### Purpose

The Phase 0 throwaway script that calls Fireworks directly.

### Why it remains useful

It separates provider-connectivity problems from proxy problems. If the proxy fails, this script can help confirm whether the provider URL, key, and request format still work directly.

It is not part of the running FastAPI server.

---

## `README.md`

### Purpose

Provides commands for initializing the database, issuing keys, running the proxy, testing with curl, revoking keys, and running automated tests.

---

## Security rules to remember

1. Real Fireworks and Anthropic keys stay only in `.env`.
2. A plaintext virtual key is displayed only once.
3. SQLite stores only SHA-256 hashes and safe metadata.
4. Complete keys and hashes must not appear in logs or CLI listings.
5. Every application receives its own virtual key.
6. Revoking one application must not affect another application.
7. A finished, failed, cancelled, or disconnected stream must close its upstream provider resources.
8. A model route selects a provider, but SQLite permission must authorize it before any real credential is resolved.
9. Unsupported cross-provider features must return a clear error instead of being silently discarded.
