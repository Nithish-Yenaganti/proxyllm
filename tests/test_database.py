"""Verify virtual-key persistence, lookup, and independent revocation."""

# Supplies temporary directories that are removed after each test.
import tempfile

# Supplies Python's asynchronous unittest support.
import unittest

# Converts a temporary directory string into a database filesystem path.
from pathlib import Path

# Creates a Phase 2-only database fixture for migration verification.
import aiosqlite

# Supplies every database operation used during the key lifecycle.
from auth.database import (
    CREATE_VIRTUAL_KEYS_TABLE,
    create_virtual_key_record,
    get_active_virtual_key_by_hash,
    get_provider_permission_for_key,
    grant_provider_permission,
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

        # Confirms Phase 2 provider fields migrated into the Phase 4 permission list.
        self.assertEqual(
            records[0]["permissions"],
            [
                {
                    "provider": "fireworks",
                    "provider_credential": "default",
                }
            ],
        )

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

    # Confirms a real Phase 2 schema gains its original permission without data loss.
    async def test_phase_two_database_is_migrated(self) -> None:
        # Uses a second database path that initialize_database has not touched yet.
        legacy_database_path = (
            Path(self.temporary_directory.name) / "phase-two.db"
        )

        # Creates only the original Phase 2 virtual_keys table.
        async with aiosqlite.connect(legacy_database_path) as database:
            # Installs the exact legacy schema retained by the current migration.
            await database.execute(CREATE_VIRTUAL_KEYS_TABLE)

            # Inserts one realistic legacy key record with Fireworks authorization.
            await database.execute(
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
                    "legacy-app",
                    "nk_legacy123",
                    "legacy-hash",
                    "fireworks",
                    "default",
                ),
            )

            # Makes the Phase 2 fixture durable before running Phase 4 migration.
            await database.commit()

        # Runs the same idempotent migration used during FastAPI startup.
        await initialize_database(legacy_database_path)

        # Looks up the automatically backfilled Fireworks permission.
        migrated_permission = await get_provider_permission_for_key(
            1,
            "fireworks",
            legacy_database_path,
        )

        # Confirms the original provider authorization survived the migration.
        self.assertEqual(
            migrated_permission,
            {
                "provider": "fireworks",
                "provider_credential": "default",
            },
        )

    # Confirms one active key can receive independent permissions for two providers.
    async def test_one_key_can_use_multiple_authorized_providers(self) -> None:
        # Generates one plaintext key that remains only in test memory.
        virtual_key = generate_virtual_key()

        # Creates the key with its initial Fireworks permission.
        record_id = await create_virtual_key_record(
            "multi-provider-app",
            get_key_prefix(virtual_key),
            hash_virtual_key(virtual_key),
            "fireworks",
            "default",
            self.database_path,
        )

        # Grants the same active key permission to use Anthropic.
        was_granted = await grant_provider_permission(
            record_id,
            "anthropic",
            "default",
            self.database_path,
        )

        # Confirms the trusted grant operation succeeded.
        self.assertTrue(was_granted)

        # Confirms model routing may resolve the original provider permission.
        self.assertIsNotNone(
            await get_provider_permission_for_key(
                record_id,
                "fireworks",
                self.database_path,
            )
        )

        # Confirms model routing may independently resolve the new permission.
        anthropic_permission = await get_provider_permission_for_key(
            record_id,
            "anthropic",
            self.database_path,
        )
        self.assertEqual(
            anthropic_permission,
            {
                "provider": "anthropic",
                "provider_credential": "default",
            },
        )

        # Confirms an ungranted provider remains unauthorized.
        self.assertIsNone(
            await get_provider_permission_for_key(
                record_id,
                "openai",
                self.database_path,
            )
        )

        # Revokes the complete virtual key after its permissions are verified.
        await revoke_virtual_key_record(record_id, self.database_path)

        # Confirms revocation disables every provider permission at once.
        self.assertIsNone(
            await get_provider_permission_for_key(
                record_id,
                "anthropic",
                self.database_path,
            )
        )

        # Confirms a revoked key cannot receive new provider permissions.
        self.assertFalse(
            await grant_provider_permission(
                record_id,
                "openai",
                "default",
                self.database_path,
            )
        )

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
