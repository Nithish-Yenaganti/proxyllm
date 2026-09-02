# ProxyLLM Code Guide

This guide explains what each project file does, why it exists, and what its functions or classes are responsible for.

## How the project works

There are three main flows.

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
Resolve its permitted provider credential
        ↓
Replace the virtual key with the real Fireworks key
        ↓
Forward the request to Fireworks
        ↓
Return the Fireworks response to the client
```

### Streaming an AI response

```text
Client sends the normal request with "stream": true
        ↓
Authentication and provider authorization run normally
        ↓
Proxy opens Fireworks with HTTPX streaming enabled
        ↓
Fireworks sends one Server-Sent Event chunk
        ↓
Proxy immediately yields the same raw bytes to the client
        ↓
Repeat until data: [DONE] or the client disconnects
        ↓
Always close the Fireworks response and HTTPX client
```

The proxy does not parse, join, or rebuild the SSE events. This preserves the provider's OpenAI-compatible streaming format and prevents the complete answer from being buffered in memory.

---

## `api/main.py`

### Purpose

This is the main FastAPI application. Uvicorn imports it to start the HTTP server.

### Why we need it

It connects the HTTP API, authentication middleware, provider permissions, and Fireworks forwarding logic.

### Important values

#### `PROVIDER_CREDENTIALS`

Maps a safe provider reference from SQLite to real provider configuration loaded from `.env`.

Example mapping:

```text
fireworks + default
        ↓
FIREWORK_API_KEY + FIREWORK_URL
```

The client never receives or directly uses the real Fireworks key.

### Functions

#### `create_provider_client()`

Creates the asynchronous HTTPX client used for one provider request.

Keeping creation in a function makes tests able to replace real networking with an in-memory provider while production continues using a normal HTTP client.

#### `build_provider_headers(provider_api_key)`

Creates the outbound provider headers.

It places the real server-side provider key in `Authorization: Bearer ...`; the incoming virtual key is never forwarded to Fireworks.

#### `provider_connection_error()`

Creates the sanitized `502 Bad Gateway` JSON response used when HTTPX cannot open a connection to Fireworks.

#### `get_provider_response_headers(provider_response)`

Copies response metadata that the client needs to interpret the body correctly, including `Content-Type` and `Cache-Control`. The streaming path also preserves `Content-Encoding` because it forwards raw provider bytes.

It deliberately does not copy `Content-Length` or connection-specific headers because a streaming response does not have a known final length when it begins.

#### `stream_provider_body(request, provider_response, provider_client)`

Asynchronously loops over `provider_response.aiter_raw()` and yields each provider byte chunk immediately.

Before yielding a chunk, it checks whether the calling client disconnected. Its `finally` block closes both the provider response and HTTPX client after normal completion, errors, cancellation, or disconnection.

#### `lifespan(_app)`

Runs when FastAPI starts.

It calls `initialize_database()` to make sure the SQLite database and table exist before requests arrive.

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
2. Finds the provider credential that key is allowed to use.
3. Reads the incoming OpenAI-compatible JSON body.
4. Checks whether the client explicitly sent `"stream": true`.
5. Uses the real provider key from `.env` when sending to Fireworks.
6. Keeps the Phase 2 complete-JSON behavior when streaming was not requested.
7. Opens an unbuffered provider response when streaming was requested.
8. Returns each SSE chunk immediately through `StreamingResponse`.

It returns:

- `403` when the virtual key is not allowed to use a configured provider.
- `500` when the server is missing provider configuration.
- `502` when Fireworks cannot be reached.

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

Creates the SQLite schema and contains every database operation for virtual keys.

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

### Functions

#### `initialize_database(database_path)`

Creates the SQLite file and `virtual_keys` table when they do not exist.

It is safe to run repeatedly because the SQL uses `CREATE TABLE IF NOT EXISTS`.

#### `create_virtual_key_record(...)`

Inserts one key's safe metadata and hash.

It never receives or stores the plaintext virtual key. It returns the new numeric record ID.

#### `list_virtual_key_records(database_path)`

Returns safe metadata for active and revoked records.

It deliberately excludes `key_hash`, so the CLI cannot accidentally display stored authentication data.

#### `get_active_virtual_key_by_hash(key_hash, database_path)`

Finds a record only when its hash matches and `is_active` is `1`.

The middleware uses this function to authenticate requests. Revoked and unknown keys both return `None`.

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

#### `revoke_key(record_id)`

Revokes one active key by its numeric database ID.

It does not affect any other application's key.

#### `run_command(arguments)`

Routes parsed CLI arguments to `create_key`, `list_keys`, or `revoke_key`.

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

Tests key storage, lookup, and revocation using temporary SQLite databases.

### Tests

- A hash and its metadata can be inserted and listed.
- Safe listings do not expose `key_hash`.
- Active hashes can be found.
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

Tests Phase 3 without reading `.env`, spending provider credit, or opening a real network port.

### Helpers

#### `TrackedByteStream`

Acts like a provider's asynchronous response body and records whether the proxy closed it.

#### `build_route_request(body)`

Builds an in-memory Starlette request and attaches the same safe provider permission metadata that authentication middleware normally supplies.

#### `DisconnectedRequest`

Simulates a client that has already disconnected so cleanup behavior can be verified.

### Tests

- SSE chunks are returned in their original order without being rewritten.
- The final `data: [DONE]` event reaches the client.
- `stream: true` and the rest of the JSON body reach the provider unchanged.
- The real provider key replaces the virtual key on the outbound request.
- Streaming response headers are preserved without adding `Content-Length`.
- Provider response and HTTPX client resources close after completion.
- A client disconnect stops forwarding and closes upstream resources.
- Requests without `stream: true` retain the complete Phase 2 JSON behavior.

---

## `tests/__init__.py`

Marks `tests` as a Python test package.

---

## `.env`

### Purpose

Stores local real-provider configuration such as the Fireworks URL and API key.

### Security rule

This file is ignored by Git and must never be committed or displayed.

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

1. The real Fireworks key stays only in `.env`.
2. A plaintext virtual key is displayed only once.
3. SQLite stores only SHA-256 hashes and safe metadata.
4. Complete keys and hashes must not appear in logs or CLI listings.
5. Every application receives its own virtual key.
6. Revoking one application must not affect another application.
7. A finished, failed, cancelled, or disconnected stream must close its upstream provider resources.
