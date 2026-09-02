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


# Creates the database file and schema without returning a value.
async def initialize_database(database_path: Path = DATABASE_PATH) -> None:
    # Opens the SQLite connection and closes it automatically afterward.
    async with aiosqlite.connect(database_path) as database:
        # Creates virtual_keys only when it does not already exist.
        await database.execute(CREATE_VIRTUAL_KEYS_TABLE)

        # Creates the Phase 4 many-provider permission table when it is missing.
        await database.execute(CREATE_PROVIDER_PERMISSIONS_TABLE)

        # Gives every existing Phase 2 key its original provider permission.
        await database.execute(BACKFILL_PROVIDER_PERMISSIONS)

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
    # Ensures authentication also works on the first server startup.
    await initialize_database(database_path)

    # Opens a read connection for this authentication attempt.
    async with aiosqlite.connect(database_path) as database:
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


# Finds the credential permission an active virtual key has for one routed provider.
async def get_provider_permission_for_key(
    record_id: int,
    provider: str,
    database_path: Path = DATABASE_PATH,
) -> dict[str, str] | None:
    # Ensures existing Phase 2 databases are migrated before authorization runs.
    await initialize_database(database_path)

    # Opens a short-lived connection for this provider authorization check.
    async with aiosqlite.connect(database_path) as database:
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
