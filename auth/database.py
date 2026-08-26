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
async def initialize_database() -> None:
    # Opens the SQLite connection and closes it automatically afterward.
    async with aiosqlite.connect(DATABASE_PATH) as database:
        # Creates virtual_keys only when it does not already exist.
        await database.execute(CREATE_VIRTUAL_KEYS_TABLE)

        # Permanently saves the schema change to the database file.
        await database.commit()


# Runs only when called directly with `python -m auth.database`.
if __name__ == "__main__":
    # Starts an event loop and waits until asynchronous initialization finishes.
    asyncio.run(initialize_database())

    # Confirms where the local database was created.
    print(f"Database initialized at {DATABASE_PATH}")
