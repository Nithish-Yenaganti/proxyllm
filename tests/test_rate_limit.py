"""Verify persistent per-key exact sliding-window request limiting."""

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiosqlite

from auth.database import create_virtual_key_record, initialize_database
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key
from auth.rate_limit import REQUESTS_PER_WINDOW, consume_rate_limit


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the limiter against an isolated real SQLite database."""

    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test.db"
        await initialize_database(self.database_path)
        self.first_key_id = await self._create_key("first-app")
        self.second_key_id = await self._create_key("second-app")

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def _create_key(self, app_name: str) -> int:
        virtual_key = generate_virtual_key()
        return await create_virtual_key_record(
            app_name,
            get_key_prefix(virtual_key),
            hash_virtual_key(virtual_key),
            "fireworks",
            "default",
            self.database_path,
        )

    async def test_allows_24_requests_and_records_no_event_for_25th(self) -> None:
        decisions = [
            await consume_rate_limit(
                self.first_key_id,
                self.database_path,
                current_time_unix=120.25,
            )
            for _ in range(REQUESTS_PER_WINDOW + 1)
        ]

        self.assertTrue(all(decision.allowed for decision in decisions[:24]))
        self.assertFalse(decisions[24].allowed)
        self.assertEqual(decisions[24].retry_after_seconds, 60)

        # A second rejected request proves exhaustion does not add another event.
        again = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=130,
        )
        self.assertFalse(again.allowed)

        async with aiosqlite.connect(self.database_path) as database:
            cursor = await database.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE virtual_key_id = ?",
                (self.first_key_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

        self.assertEqual(row, (REQUESTS_PER_WINDOW,))

    async def test_separate_keys_have_independent_capacity(self) -> None:
        for _ in range(REQUESTS_PER_WINDOW):
            decision = await consume_rate_limit(
                self.first_key_id,
                self.database_path,
                current_time_unix=240,
            )
            self.assertTrue(decision.allowed)

        first_excess = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=240,
        )
        second_first = await consume_rate_limit(
            self.second_key_id,
            self.database_path,
            current_time_unix=240,
        )

        self.assertFalse(first_excess.allowed)
        self.assertTrue(second_first.allowed)
        self.assertEqual(second_first.remaining, REQUESTS_PER_WINDOW - 1)

    async def test_capacity_rolls_forward_as_old_requests_expire(self) -> None:
        for _ in range(12):
            await consume_rate_limit(
                self.first_key_id,
                self.database_path,
                current_time_unix=300,
            )

        for _ in range(12):
            await consume_rate_limit(
                self.first_key_id,
                self.database_path,
                current_time_unix=330,
            )

        exhausted = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=359.5,
        )
        after_oldest_expires = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=360,
        )
        after_second_group_expires = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=390,
        )

        self.assertFalse(exhausted.allowed)
        self.assertEqual(exhausted.retry_after_seconds, 1)
        self.assertTrue(after_oldest_expires.allowed)
        self.assertEqual(after_oldest_expires.remaining, 11)
        self.assertTrue(after_second_group_expires.allowed)
        self.assertEqual(after_second_group_expires.remaining, 22)

    async def test_crossing_a_fixed_minute_does_not_create_a_burst(self) -> None:
        for _ in range(REQUESTS_PER_WINDOW):
            await consume_rate_limit(
                self.first_key_id,
                self.database_path,
                current_time_unix=119.9,
            )

        just_after_next_minute = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=120.1,
        )

        self.assertFalse(just_after_next_minute.allowed)
        self.assertEqual(just_after_next_minute.retry_after_seconds, 60)

    async def test_concurrent_requests_cannot_exceed_limit(self) -> None:
        decisions = await asyncio.gather(
            *(
                consume_rate_limit(
                    self.first_key_id,
                    self.database_path,
                    current_time_unix=480,
                )
                for _ in range(REQUESTS_PER_WINDOW + 16)
            )
        )

        self.assertEqual(sum(decision.allowed for decision in decisions), 24)
        self.assertEqual(sum(not decision.allowed for decision in decisions), 16)

    async def test_accepted_events_persist_across_database_connections(self) -> None:
        first = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=600,
        )
        second = await consume_rate_limit(
            self.first_key_id,
            self.database_path,
            current_time_unix=600,
        )

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertEqual(first.remaining, 23)
        self.assertEqual(second.remaining, 22)


if __name__ == "__main__":
    unittest.main()
