"""Gateway cache deadlines, including entries made under the old policy."""
import unittest
from unittest.mock import AsyncMock, patch
from decimal import Decimal
from api import main


class CacheTTLTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_entry_expires_at_thirty_minutes(self):
        record = {"created_at_unix": 1000, "expires_at_unix": 4600}
        for now, expected in [(2799, record), (2800, None), (3000, None)]:
            with patch.object(main, "time", return_value=now), patch.object(
                main, "get_cached_response_record", new_callable=AsyncMock,
                return_value=record,
            ):
                self.assertEqual(await main.read_cached_response("test"), expected)

    async def test_new_entry_has_thirty_minute_deadline(self):
        with patch.object(main, "time", return_value=1000), patch.object(
            main, "upsert_cached_response_record", new_callable=AsyncMock,
        ) as write:
            await main.write_cached_response(
                "test", 1, "fireworks", "test-model", 200, b"{}", {}, Decimal("0"),
            )
        self.assertEqual(write.call_args.kwargs["expires_at_unix"], 2800)
