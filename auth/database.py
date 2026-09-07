"""Create the local SQLite database used for virtual-key authentication."""

# Runs the asynchronous setup function when this file is used as a CLI module.
import asyncio

# Builds a reliable database path relative to this file, not the current terminal folder.
from pathlib import Path

# Provides non-blocking SQLite access that works with our asynchronous FastAPI app.
import aiosqlite


# Stores the database beside this module as auth/gateway.db.
DATABASE_PATH = Path(__file__).resolve().parent / "gateway.db"


# Defines the table that will hold safe virtual-key metadata and hashes.
# id: internal numeric identifier for each database record.
# app_name: human-readable label showing which application owns the key.
# key_prefix: short, non-secret portion used to recognize a key later.
# key_hash: one-way SHA-256 result used for authentication instead of plaintext.
# provider: provider this key is authorized to access, such as fireworks.
# provider_credential: safe name of the provider credential to resolve from configuration.
# is_active: 1 permits requests and 0 marks the virtual key as revoked.
# created_at: timestamp automatically recorded when the key is inserted.
# revoked_at: optional timestamp recording when access was revoked.
CREATE_VIRTUAL_KEYS_TABLE = """
CREATE TABLE IF NOT EXISTS virtual_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    provider_credential TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT
)
"""


# Defines provider permissions separately so one virtual key can use multiple providers.
# key_id: virtual key receiving this permission.
# provider: provider selected by the requested model route.
# provider_credential: trusted server-side credential name that may be resolved.
CREATE_PROVIDER_PERMISSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS virtual_key_provider_permissions (
    key_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_credential TEXT NOT NULL,
    PRIMARY KEY (key_id, provider),
    FOREIGN KEY (key_id) REFERENCES virtual_keys(id) ON DELETE CASCADE
)
"""


# Stores one timestamp for each request admitted inside a key's rolling window.
CREATE_RATE_LIMIT_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_key_id INTEGER NOT NULL,
    accepted_at_unix REAL NOT NULL,
    FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(id) ON DELETE CASCADE
)
"""


# Makes per-key pruning, counting, and oldest-request lookup use one ordered index.
CREATE_RATE_LIMIT_EVENTS_KEY_TIME_INDEX = """
CREATE INDEX IF NOT EXISTS idx_rate_limit_events_key_time
ON rate_limit_events (virtual_key_id, accepted_at_unix)
"""


# Migrates each Phase 2 key's original provider fields into its initial permission.
BACKFILL_PROVIDER_PERMISSIONS = """
INSERT OR IGNORE INTO virtual_key_provider_permissions (
    key_id,
    provider,
    provider_credential
)
SELECT
    id,
    provider,
    provider_credential
FROM virtual_keys
"""


# Defines one immutable accounting record for every authenticated chat request.
# virtual_key_id identifies the application without storing its plaintext secret.
# provider and model may be null when validation fails before routing can finish.
# token columns contain normalized OpenAI-compatible usage counters.
# estimated_cost_usd stores the model-price estimate calculated when the call ran.
# latency_ms measures gateway time from route entry through response completion.
# status provides a searchable outcome such as success, denied, or provider_error.
# status_code preserves the HTTP status returned to the calling application.
CREATE_USAGE_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_key_id INTEGER NOT NULL,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    cached_prompt_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (cached_prompt_tokens >= 0),
    completion_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (completion_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    estimated_cost_usd REAL NOT NULL DEFAULT 0
        CHECK (estimated_cost_usd >= 0),
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
    status TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    cache_status TEXT NOT NULL DEFAULT 'not_eligible',
    cost_avoided_usd REAL NOT NULL DEFAULT 0
        CHECK (cost_avoided_usd >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(id)
)
"""


# Stores complete successful responses for deterministic, non-streaming requests.
CREATE_RESPONSE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key TEXT PRIMARY KEY,
    virtual_key_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    response_status_code INTEGER NOT NULL,
    response_body BLOB NOT NULL,
    response_headers_json TEXT NOT NULL,
    estimated_cost_usd REAL NOT NULL CHECK (estimated_cost_usd >= 0),
    created_at_unix REAL NOT NULL,
    expires_at_unix REAL NOT NULL,
    FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(id) ON DELETE CASCADE
)
"""


# Makes per-app cache cleanup and expiry maintenance efficient.
CREATE_RESPONSE_CACHE_EXPIRY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_response_cache_expiry
ON response_cache (virtual_key_id, expires_at_unix)
"""


# Speeds later per-app and per-day Phase 5 reports without duplicating log data.
CREATE_USAGE_LOGS_KEY_DATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_usage_logs_key_date
ON usage_logs (virtual_key_id, created_at)
"""


# Applies safety and concurrency settings to each newly opened SQLite connection.
async def configure_database_connection(database: aiosqlite.Connection) -> None:
    # Enforces key ownership relationships instead of accepting orphan rows.
    await database.execute("PRAGMA foreign_keys = ON")

    # Waits briefly for another request's write transaction instead of failing at once.
    await database.execute("PRAGMA busy_timeout = 5000")


# Adds one migration column only when an older database does not contain it.
async def add_column_if_missing(
    database: aiosqlite.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    # Reads SQLite's trusted schema metadata for the fixed internal table name.
    cursor = await database.execute(f"PRAGMA table_info({table_name})")

    # Collects every existing column name before deciding whether to migrate.
    existing_columns = {row[1] for row in await cursor.fetchall()}

    # Releases the schema cursor after its small result set has been read.
    await cursor.close()

    # Leaves current databases unchanged when the column already exists.
    if column_name in existing_columns:
        # Makes repeated application startups idempotent.
        return

    # Applies a fixed developer-controlled definition, never client-supplied SQL.
    await database.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
    )


# Creates the database file and schema without returning a value.
async def initialize_database(database_path: Path = DATABASE_PATH) -> None:
    # Opens the SQLite connection and closes it automatically afterward.
    async with aiosqlite.connect(database_path) as database:
        # Applies foreign-key enforcement and a concurrency-friendly lock timeout.
        await configure_database_connection(database)

        # Enables concurrent readers while one request appends a log or cache record.
        await database.execute("PRAGMA journal_mode = WAL")

        # Creates virtual_keys only when it does not already exist.
        await database.execute(CREATE_VIRTUAL_KEYS_TABLE)

        # Creates the Phase 4 many-provider permission table when it is missing.
        await database.execute(CREATE_PROVIDER_PERMISSIONS_TABLE)

        # Gives every existing Phase 2 key its original provider permission.
        await database.execute(BACKFILL_PROVIDER_PERMISSIONS)

        # Creates persistent accepted-request timestamps for rolling-window throttling.
        await database.execute(CREATE_RATE_LIMIT_EVENTS_TABLE)
        await database.execute(CREATE_RATE_LIMIT_EVENTS_KEY_TIME_INDEX)

        # Creates Phase 5.1 request accounting without changing existing key records.
        await database.execute(CREATE_USAGE_LOGS_TABLE)

        # Adds Phase 5.2 cache metadata to databases created before caching existed.
        await add_column_if_missing(
            database,
            "usage_logs",
            "cache_status",
            "TEXT NOT NULL DEFAULT 'not_eligible'",
        )

        # Adds avoided-cost accounting without rewriting historical usage rows.
        await add_column_if_missing(
            database,
            "usage_logs",
            "cost_avoided_usd",
            "REAL NOT NULL DEFAULT 0 CHECK (cost_avoided_usd >= 0)",
        )

        # Creates the persistent per-application response cache when missing.
        await database.execute(CREATE_RESPONSE_CACHE_TABLE)

        # Adds the reporting index only when it has not already been created.
        await database.execute(CREATE_USAGE_LOGS_KEY_DATE_INDEX)

        # Adds the cache expiry lookup index once for future request hot paths.
        await database.execute(CREATE_RESPONSE_CACHE_EXPIRY_INDEX)

        # Permanently saves the schema change to the database file.
        await database.commit()


# Inserts one virtual-key hash and returns its generated numeric record ID.
async def create_virtual_key_record(
    app_name: str,
    key_prefix: str,
    key_hash: str,
    provider: str,
    provider_credential: str,
    database_path: Path = DATABASE_PATH,
) -> int:
    # Ensures the table exists before attempting the insert.
    await initialize_database(database_path)

    # Opens a short-lived asynchronous connection for this write operation.
    async with aiosqlite.connect(database_path) as database:
        # Inserts only metadata and the one-way hash, never the plaintext key.
        cursor = await database.execute(
            """
            INSERT INTO virtual_keys (
                app_name,
                key_prefix,
                key_hash,
                provider,
                provider_credential
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                app_name,
                key_prefix,
                key_hash,
                provider,
                provider_credential,
            ),
        )

        # Reads SQLite's ID before adding the key's initial provider permission.
        record_id = cursor.lastrowid

        # Guards against an unexpected driver result before using the ID as a foreign key.
        if record_id is None:
            # Closes the insert cursor before reporting the persistence failure.
            await cursor.close()

            # Reports an internal failure to the trusted CLI caller.
            raise RuntimeError("SQLite did not return an ID for the virtual key")

        # Adds the provider permission selected when this key was first issued.
        await database.execute(
            """
            INSERT INTO virtual_key_provider_permissions (
                key_id,
                provider,
                provider_credential
            )
            VALUES (?, ?, ?)
            """,
            (
                record_id,
                provider,
                provider_credential,
            ),
        )

        # Makes the key record and its initial permission durable together.
        await database.commit()

        # Closes the cursor now that its generated ID has been captured.
        await cursor.close()

    # Returns the stable identifier used later for revocation.
    return record_id


# Returns safe metadata for every issued key without exposing hashes or secrets.
async def list_virtual_key_records(
    database_path: Path = DATABASE_PATH,
) -> list[dict[str, object]]:
    # Ensures a new installation has the expected table.
    await initialize_database(database_path)

    # Opens a read connection for the listing operation.
    async with aiosqlite.connect(database_path) as database:
        # Makes each SQLite row accessible by its column name.
        database.row_factory = aiosqlite.Row

        # Selects only metadata that is safe to display in an administrator CLI.
        cursor = await database.execute(
            """
            SELECT
                id,
                app_name,
                key_prefix,
                provider,
                provider_credential,
                is_active,
                created_at,
                revoked_at
            FROM virtual_keys
            ORDER BY id
            """
        )

        # Loads all safe records before the connection closes.
        rows = await cursor.fetchall()

        # Closes the cursor after reading its result set.
        await cursor.close()

        # Loads every safe provider permission for the listed keys.
        permissions_cursor = await database.execute(
            """
            SELECT
                key_id,
                provider,
                provider_credential
            FROM virtual_key_provider_permissions
            ORDER BY key_id, provider
            """
        )

        # Reads permission metadata before the database connection closes.
        permission_rows = await permissions_cursor.fetchall()

        # Releases the second read cursor after loading its rows.
        await permissions_cursor.close()

    # Groups each permission under the virtual key that owns it.
    permissions_by_key: dict[int, list[dict[str, str]]] = {}

    # Visits every safe permission returned by SQLite.
    for permission_row in permission_rows:
        # Converts the driver row to an ordinary dictionary.
        permission = dict(permission_row)

        # Reads the numeric key ID used only for grouping.
        key_id = int(permission.pop("key_id"))

        # Creates the key's list when needed and appends this provider permission.
        permissions_by_key.setdefault(key_id, []).append(permission)

    # Converts key rows to dictionaries and attaches their safe permission lists.
    records: list[dict[str, object]] = []

    # Visits every issued virtual key in stable database order.
    for row in rows:
        # Converts this driver-specific row to an ordinary dictionary.
        record = dict(row)

        # Adds every configured provider permission without exposing key hashes.
        record["permissions"] = permissions_by_key.get(int(record["id"]), [])

        # Adds the completed safe record to the CLI result.
        records.append(record)

    # Returns all safe key metadata and provider permissions.
    return records


# Finds one active key by hash and returns its authorization metadata.
async def get_active_virtual_key_by_hash(
    key_hash: str,
    database_path: Path = DATABASE_PATH,
) -> dict[str, object] | None:
    # Opens a read connection for this authentication attempt.
    async with aiosqlite.connect(database_path) as database:
        # Uses startup-created schema and waits safely around concurrent writes.
        await configure_database_connection(database)

        # Makes the selected row accessible by descriptive column names.
        database.row_factory = aiosqlite.Row

        # Requires both a matching hash and an active status.
        cursor = await database.execute(
            """
            SELECT
                id,
                app_name,
                key_prefix,
                provider,
                provider_credential,
                is_active,
                created_at
            FROM virtual_keys
            WHERE key_hash = ? AND is_active = 1
            LIMIT 1
            """,
            (key_hash,),
        )

        # Reads the matching record or None when authentication fails.
        row = await cursor.fetchone()

        # Closes the cursor after the single-row lookup.
        await cursor.close()

    # Returns ordinary metadata to callers while keeping the stored hash private.
    return dict(row) if row is not None else None


# Inserts one completed authenticated request into the append-only usage ledger.
async def create_usage_log_record(
    virtual_key_id: int,
    provider: str | None,
    model: str | None,
    prompt_tokens: int,
    cached_prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    status: str,
    status_code: int,
    cache_status: str = "not_eligible",
    cost_avoided_usd: float = 0,
    database_path: Path = DATABASE_PATH,
) -> int:
    # Opens one short-lived write connection for this completed request.
    async with aiosqlite.connect(database_path) as database:
        # Waits safely when another concurrent request is writing to SQLite.
        await configure_database_connection(database)

        # Inserts normalized metrics and safe identifiers without request content or keys.
        cursor = await database.execute(
            """
            INSERT INTO usage_logs (
                virtual_key_id,
                provider,
                model,
                prompt_tokens,
                cached_prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                latency_ms,
                status,
                status_code,
                cache_status,
                cost_avoided_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                virtual_key_id,
                provider,
                model,
                prompt_tokens,
                cached_prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                latency_ms,
                status,
                status_code,
                cache_status,
                cost_avoided_usd,
            ),
        )

        # Makes this accounting record durable before returning control.
        await database.commit()

        # Captures the stable log identifier generated by SQLite.
        log_id = cursor.lastrowid

        # Releases the write cursor after its ID has been read.
        await cursor.close()

    # Guards against an unexpected database driver result.
    if log_id is None:
        # Reports persistence failure to the internal logging caller.
        raise RuntimeError("SQLite did not return an ID for the usage log")

    # Returns the identifier used by tests and future administrative tooling.
    return log_id


async def summarize_usage(
    virtual_key_id: int | None = None,
    database_path: Path = DATABASE_PATH,
) -> list[dict[str, object]]:
    """Aggregate all-time recorded usage per key, including revoked keys."""
    await initialize_database(database_path)
    async with aiosqlite.connect(database_path) as database:
        database.row_factory = aiosqlite.Row
        cursor = await database.execute(
            """
            SELECT k.id AS virtual_key_id, k.app_name,
                   COUNT(u.id) AS requests,
                   COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(u.estimated_cost_usd), 0) AS estimated_cost_usd,
                   COALESCE(SUM(u.cost_avoided_usd), 0) AS cost_avoided_usd,
                   COALESCE(SUM(CASE WHEN u.cache_status = 'hit' THEN 1 ELSE 0 END), 0)
                       AS cache_hits
            FROM virtual_keys k
            LEFT JOIN usage_logs u ON u.virtual_key_id = k.id
            WHERE (? IS NULL OR k.id = ?)
            GROUP BY k.id, k.app_name
            ORDER BY k.id
            """,
            (virtual_key_id, virtual_key_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


# Returns stored usage rows for tests and administrative inspection.
async def list_usage_log_records(
    database_path: Path = DATABASE_PATH,
) -> list[dict[str, object]]:
    # Ensures the table exists before reading a fresh installation.
    await initialize_database(database_path)

    # Opens a read-only-style connection for the accounting ledger query.
    async with aiosqlite.connect(database_path) as database:
        # Allows callers to address every result by its descriptive column name.
        database.row_factory = aiosqlite.Row

        # Reads logs in deterministic insertion order for reports and tests.
        cursor = await database.execute(
            """
            SELECT
                id,
                virtual_key_id,
                provider,
                model,
                prompt_tokens,
                cached_prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                latency_ms,
                status,
                status_code,
                cache_status,
                cost_avoided_usd,
                created_at
            FROM usage_logs
            ORDER BY id
            """
        )

        # Loads the small result set before closing its connection.
        rows = await cursor.fetchall()

        # Releases the query cursor after all rows are available.
        await cursor.close()

    # Converts driver-specific rows into ordinary dictionaries.
    return [dict(row) for row in rows]


# Returns one unexpired cached response for an exact per-application cache key.
async def get_cached_response_record(
    cache_key: str,
    current_time_unix: float,
    database_path: Path = DATABASE_PATH,
) -> dict[str, object] | None:
    # Opens one short-lived read connection on the initialized application database.
    async with aiosqlite.connect(database_path) as database:
        # Applies the same concurrency and relationship settings as request writes.
        await configure_database_connection(database)

        # Makes the optional cache row accessible by descriptive names.
        database.row_factory = aiosqlite.Row

        # Requires both an exact key match and a future expiry timestamp.
        cursor = await database.execute(
            """
            SELECT
                cache_key,
                virtual_key_id,
                provider,
                model,
                response_status_code,
                response_body,
                response_headers_json,
                estimated_cost_usd,
                created_at_unix,
                expires_at_unix
            FROM response_cache
            WHERE cache_key = ? AND expires_at_unix > ?
            LIMIT 1
            """,
            (cache_key, current_time_unix),
        )

        # Reads the cached response or None when it is missing or expired.
        row = await cursor.fetchone()

        # Releases the lookup cursor after the single-row read.
        await cursor.close()

    # Returns an ordinary dictionary without exposing SQLite row objects upstream.
    return dict(row) if row is not None else None


# Creates or refreshes one successful deterministic response cache entry.
async def upsert_cached_response_record(
    cache_key: str,
    virtual_key_id: int,
    provider: str,
    model: str,
    response_status_code: int,
    response_body: bytes,
    response_headers_json: str,
    estimated_cost_usd: float,
    created_at_unix: float,
    expires_at_unix: float,
    database_path: Path = DATABASE_PATH,
) -> None:
    # Opens one bounded write connection for this cacheable provider response.
    async with aiosqlite.connect(database_path) as database:
        # Waits for concurrent usage-log writes instead of failing immediately.
        await configure_database_connection(database)

        # Inserts a new value or atomically refreshes the same normalized request key.
        cursor = await database.execute(
            """
            INSERT INTO response_cache (
                cache_key,
                virtual_key_id,
                provider,
                model,
                response_status_code,
                response_body,
                response_headers_json,
                estimated_cost_usd,
                created_at_unix,
                expires_at_unix
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cache_key)
            DO UPDATE SET
                response_status_code = excluded.response_status_code,
                response_body = excluded.response_body,
                response_headers_json = excluded.response_headers_json,
                estimated_cost_usd = excluded.estimated_cost_usd,
                created_at_unix = excluded.created_at_unix,
                expires_at_unix = excluded.expires_at_unix
            """,
            (
                cache_key,
                virtual_key_id,
                provider,
                model,
                response_status_code,
                response_body,
                response_headers_json,
                estimated_cost_usd,
                created_at_unix,
                expires_at_unix,
            ),
        )

        # Makes the response available to later gateway requests immediately.
        await database.commit()

        # Releases the cache write cursor after the transaction commits.
        await cursor.close()


# Finds the credential permission an active virtual key has for one routed provider.
async def get_provider_permission_for_key(
    record_id: int,
    provider: str,
    database_path: Path = DATABASE_PATH,
) -> dict[str, str] | None:
    # Opens a short-lived connection for this provider authorization check.
    async with aiosqlite.connect(database_path) as database:
        # Uses startup-created schema and waits safely around concurrent writes.
        await configure_database_connection(database)

        # Makes the matching permission accessible by column name.
        database.row_factory = aiosqlite.Row

        # Requires both an active key and an explicit permission for this provider.
        cursor = await database.execute(
            """
            SELECT
                permissions.provider,
                permissions.provider_credential
            FROM virtual_key_provider_permissions AS permissions
            JOIN virtual_keys AS keys ON keys.id = permissions.key_id
            WHERE
                keys.id = ?
                AND keys.is_active = 1
                AND permissions.provider = ?
            LIMIT 1
            """,
            (record_id, provider),
        )

        # Reads the authorized credential or None when access is not allowed.
        row = await cursor.fetchone()

        # Releases the authorization cursor after its single-row lookup.
        await cursor.close()

    # Returns safe permission metadata without exposing the real provider key.
    return dict(row) if row is not None else None


# Grants or updates one provider permission for an existing active virtual key.
async def grant_provider_permission(
    record_id: int,
    provider: str,
    provider_credential: str,
    database_path: Path = DATABASE_PATH,
) -> bool:
    # Ensures the key and permission tables exist before the trusted CLI writes.
    await initialize_database(database_path)

    # Opens one transaction for the active-key check and permission write.
    async with aiosqlite.connect(database_path) as database:
        # Checks that the target key exists and has not been revoked.
        key_cursor = await database.execute(
            """
            SELECT id
            FROM virtual_keys
            WHERE id = ? AND is_active = 1
            LIMIT 1
            """,
            (record_id,),
        )

        # Reads the active key marker before deciding whether to grant access.
        key_row = await key_cursor.fetchone()

        # Releases the lookup cursor before the write operation.
        await key_cursor.close()

        # Refuses to modify unknown or revoked key records.
        if key_row is None:
            # Reports failure without creating an orphan permission.
            return False

        # Creates the permission or updates its named credential idempotently.
        permission_cursor = await database.execute(
            """
            INSERT INTO virtual_key_provider_permissions (
                key_id,
                provider,
                provider_credential
            )
            VALUES (?, ?, ?)
            ON CONFLICT (key_id, provider)
            DO UPDATE SET provider_credential = excluded.provider_credential
            """,
            (
                record_id,
                provider,
                provider_credential,
            ),
        )

        # Makes the new or updated authorization durable immediately.
        await database.commit()

        # Releases the permission cursor after the committed write.
        await permission_cursor.close()

    # Confirms the active key now has the requested provider permission.
    return True


# Marks one key inactive and records its revocation time.
async def revoke_virtual_key_record(
    record_id: int,
    database_path: Path = DATABASE_PATH,
) -> bool:
    # Ensures the table exists before attempting an update.
    await initialize_database(database_path)

    # Opens a write connection for the revocation operation.
    async with aiosqlite.connect(database_path) as database:
        # Revokes only an existing key that is currently active.
        cursor = await database.execute(
            """
            UPDATE virtual_keys
            SET is_active = 0, revoked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND is_active = 1
            """,
            (record_id,),
        )

        # Makes the revocation durable immediately.
        await database.commit()

        # Captures whether SQLite actually changed a record.
        was_revoked = cursor.rowcount == 1

        # Closes the cursor after inspecting the update count.
        await cursor.close()

    # Lets the CLI distinguish success from an unknown or already-revoked ID.
    return was_revoked


# Runs only when called directly with `python -m auth.database`.
if __name__ == "__main__":
    # Starts an event loop and waits until asynchronous initialization finishes.
    asyncio.run(initialize_database())

    # Confirms where the local database was created.
    print(f"Database initialized at {DATABASE_PATH}")
