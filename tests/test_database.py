"""Verify virtual-key persistence, lookup, and independent revocation."""

# Supplies temporary directories that are removed after each test.
import tempfile

# Supplies Python's asynchronous unittest support.
import unittest

# Converts a temporary directory string into a database filesystem path.
from pathlib import Path

# Supplies every database operation used during the key lifecycle.
from auth.database import (
    create_virtual_key_record,
    get_active_virtual_key_by_hash,
    initialize_database,
    list_virtual_key_records,
    revoke_virtual_key_record,
)

# Supplies safe fixture generation and transformation helpers.
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key


# Runs each database test with async support and isolated storage.
class VirtualKeyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Creates a fresh temporary database before each test method.
    async def asyncSetUp(self) -> None:
        # Owns the temporary directory until asyncTearDown runs.
        self.temporary_directory = tempfile.TemporaryDirectory()

        # Places this test's SQLite file outside the real auth directory.
        self.database_path = Path(self.temporary_directory.name) / "test.db"

        # Creates the schema needed by the test.
        await initialize_database(self.database_path)

    # Removes the isolated database after each test method.
    async def asyncTearDown(self) -> None:
        # Deletes the temporary directory and all of its contents.
        self.temporary_directory.cleanup()

    # Confirms insertion stores only safe fields and supports active lookup.
    async def test_create_list_and_lookup_key(self) -> None:
        # Generates one plaintext fixture that remains only in test memory.
        virtual_key = generate_virtual_key()

        # Produces the database-safe hash for this fixture.
        key_hash = hash_virtual_key(virtual_key)

        # Inserts safe metadata and captures the generated ID.
        record_id = await create_virtual_key_record(
            app_name="jan",
            key_prefix=get_key_prefix(virtual_key),
            key_hash=key_hash,
            provider="fireworks",
            provider_credential="default",
            database_path=self.database_path,
        )

        # Loads safe listing metadata from the isolated database.
        records = await list_virtual_key_records(self.database_path)

        # Confirms exactly one key was issued.
        self.assertEqual(len(records), 1)

        # Confirms the returned ID matches SQLite's insert result.
        self.assertEqual(records[0]["id"], record_id)

        # Confirms listing metadata does not contain the secret hash.
        self.assertNotIn("key_hash", records[0])

        # Looks up the key the same way authentication middleware will.
        active_record = await get_active_virtual_key_by_hash(
            key_hash,
            self.database_path,
        )

        # Confirms the active fixture authenticates successfully.
        self.assertIsNotNone(active_record)

        # Narrows the optional type after the assertion for the next check.
        assert active_record is not None

        # Confirms the provider permission survived persistence.
        self.assertEqual(active_record["provider"], "fireworks")

    # Confirms revoking one application does not affect another application.
    async def test_revocation_is_independent(self) -> None:
        # Generates the first application's plaintext key.
        first_key = generate_virtual_key()

        # Generates the second application's independent plaintext key.
        second_key = generate_virtual_key()

        # Hashes the first key before persistence.
        first_hash = hash_virtual_key(first_key)

        # Hashes the second key before persistence.
        second_hash = hash_virtual_key(second_key)

        # Inserts the first application's safe metadata.
        first_id = await create_virtual_key_record(
            "jan",
            get_key_prefix(first_key),
            first_hash,
            "fireworks",
            "default",
            self.database_path,
        )

        # Inserts the second application's safe metadata.
        await create_virtual_key_record(
            "second-app",
            get_key_prefix(second_key),
            second_hash,
            "fireworks",
            "default",
            self.database_path,
        )

        # Revokes only the first application's record.
        was_revoked = await revoke_virtual_key_record(
            first_id,
            self.database_path,
        )

        # Confirms SQLite changed the requested record.
        self.assertTrue(was_revoked)

        # Confirms the revoked key can no longer be found as active.
        self.assertIsNone(
            await get_active_virtual_key_by_hash(first_hash, self.database_path)
        )

        # Confirms the second key remains independently active.
        self.assertIsNotNone(
            await get_active_virtual_key_by_hash(second_hash, self.database_path)
        )


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest's test discovery and result reporting.
    unittest.main()
