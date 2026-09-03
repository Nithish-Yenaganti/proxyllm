"""Verify Phase 5.2 cache eligibility, key privacy, and SQLite persistence."""

# Supplies isolated asynchronous tests and automatically removed directories.
import tempfile
import unittest

# Encodes safe response headers exactly as the cache database expects them.
import json

# Builds temporary SQLite paths that never touch the real gateway database.
from pathlib import Path

# Supplies cache persistence and virtual-key owner operations.
from auth.database import (
    create_virtual_key_record,
    get_cached_response_record,
    initialize_database,
    upsert_cached_response_record,
)

# Supplies safe key metadata helpers for the cache owner fixture.
from auth.keys import get_key_prefix, hash_virtual_key

# Supplies request eligibility, normalization, and hashing behavior under test.
from usage.cache import build_cache_key, build_provider_body, is_cache_eligible


# Exercises the pure cache rules without HTTP or SQLite.
class CachePolicyTests(unittest.TestCase):
    # Confirms deterministic or explicitly opted-in complete requests are eligible.
    def test_only_safe_complete_requests_are_cache_eligible(self) -> None:
        # Automatically accepts an explicitly deterministic temperature.
        self.assertTrue(is_cache_eligible({"temperature": 0}))

        # Accepts an application's explicit semantic decision.
        self.assertTrue(is_cache_eligible({"temperature": 0.7, "cache": True}))

        # Rejects creative defaults without explicit opt-in.
        self.assertFalse(is_cache_eligible({"temperature": 0.7}))

        # Gives explicit opt-out priority over deterministic temperature.
        self.assertFalse(is_cache_eligible({"temperature": 0, "cache": False}))

        # Never caches an SSE response even when the application opts in.
        self.assertFalse(is_cache_eligible({"stream": True, "cache": True}))

    # Confirms key order cannot create duplicate entries and apps cannot share output.
    def test_cache_key_is_normalized_and_scoped_to_virtual_key(self) -> None:
        # Creates two semantically identical dictionaries with different insertion order.
        first_body = {"model": "example", "temperature": 0, "messages": []}
        reordered_body = {"messages": [], "temperature": 0, "model": "example"}

        # Confirms canonical JSON creates one stable key for the same application.
        self.assertEqual(
            build_cache_key(1, first_body),
            build_cache_key(1, reordered_body),
        )

        # Confirms another application's identical prompt receives a different key.
        self.assertNotEqual(
            build_cache_key(1, first_body),
            build_cache_key(2, first_body),
        )

    # Confirms gateway-only controls never reach a real provider.
    def test_provider_body_removes_private_cache_control(self) -> None:
        # Builds one explicit cache request received by the gateway.
        original = {"model": "example", "cache": True, "messages": []}

        # Produces the independent body passed to a provider adapter.
        provider_body = build_provider_body(original)

        # Confirms provider compatibility and no mutation of incoming data.
        self.assertNotIn("cache", provider_body)
        self.assertTrue(original["cache"])


# Exercises persistent cache records in a fresh SQLite database.
class CacheDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Creates one isolated database and virtual-key owner before each test.
    async def asyncSetUp(self) -> None:
        # Owns a temporary directory until cleanup runs.
        self.temporary_directory = tempfile.TemporaryDirectory()

        # Places every test table outside the real auth directory.
        self.database_path = Path(self.temporary_directory.name) / "cache.db"

        # Creates all current schema tables and indexes.
        await initialize_database(self.database_path)

        # Uses a deterministic fixture key that remains only in test memory.
        virtual_key = "nk_cache-test_y"

        # Creates the owner required by the response-cache foreign key.
        self.virtual_key_id = await create_virtual_key_record(
            app_name="cache-test-app",
            key_prefix=get_key_prefix(virtual_key),
            key_hash=hash_virtual_key(virtual_key),
            provider="fireworks",
            provider_credential="default",
            database_path=self.database_path,
        )

    # Removes every temporary SQLite artifact after each test.
    async def asyncTearDown(self) -> None:
        # Deletes only the directory created by this test instance.
        self.temporary_directory.cleanup()

    # Confirms one cache entry is readable before expiry and absent afterward.
    async def test_cache_record_round_trip_and_expiry(self) -> None:
        # Builds a realistic privacy-preserving request hash.
        cache_key = build_cache_key(
            self.virtual_key_id,
            {"model": "fireworks/example", "messages": [], "temperature": 0},
        )

        # Stores one successful provider response for a fixed time window.
        await upsert_cached_response_record(
            cache_key=cache_key,
            virtual_key_id=self.virtual_key_id,
            provider="fireworks",
            model="fireworks/example",
            response_status_code=200,
            response_body=b'{"answer":"cached"}',
            response_headers_json=json.dumps(
                {"Content-Type": "application/json"}
            ),
            estimated_cost_usd=0.000012,
            created_at_unix=1000,
            expires_at_unix=1060,
            database_path=self.database_path,
        )

        # Reads the record inside its TTL window.
        cached = await get_cached_response_record(
            cache_key,
            1059,
            self.database_path,
        )

        # Confirms the complete response and avoided-cost estimate survived storage.
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached["response_body"], b'{"answer":"cached"}')
        self.assertEqual(cached["response_status_code"], 200)
        self.assertAlmostEqual(cached["estimated_cost_usd"], 0.000012)

        # Confirms an expired entry behaves as a miss without needing deletion first.
        self.assertIsNone(
            await get_cached_response_record(
                cache_key,
                1060,
                self.database_path,
            )
        )


# Runs this module directly with normal unittest discovery behavior.
if __name__ == "__main__":
    # Starts all cache tests and prints their results.
    unittest.main()
