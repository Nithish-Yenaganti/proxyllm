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


# Creates the database file and schema without returning a value.
async def initialize_database(database_path: Path = DATABASE_PATH) -> None:
    # Opens the SQLite connection and closes it automatically afterward.
    async with aiosqlite.connect(database_path) as database:
        # Creates virtual_keys only when it does not already exist.
        await database.execute(CREATE_VIRTUAL_KEYS_TABLE)

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

        # Makes the new record durable on disk.
        await database.commit()

        # Reads SQLite's ID for the row that was just created.
        record_id = cursor.lastrowid

        # Closes the cursor now that its generated ID has been captured.
        await cursor.close()

    # Guards against an unexpected driver result even though SQLite supplies an ID.
    if record_id is None:
        # Reports an internal persistence failure to the CLI caller.
        raise RuntimeError("SQLite did not return an ID for the virtual key")

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

    # Converts driver-specific Row objects into ordinary dictionaries.
    return [dict(row) for row in rows]


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
