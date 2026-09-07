"""Usage reports aggregate isolated fixture data without provider calls."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from auth.cli import build_parser, run_command
from auth.database import create_virtual_key_record, create_usage_log_record, summarize_usage


class UsageReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregation_filter_and_empty_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            self.assertEqual(await summarize_usage(database_path=path), [])
            ids = []
            for n in range(2):
                ids.append(await create_virtual_key_record(
                    "same-app", f"prefix{n}", f"hash{n}", "fireworks", "default", path
                ))
            for cache, cost, avoided in [("miss", 0.01, 0), ("hit", 0, 0.01)]:
                await create_usage_log_record(
                    ids[0], "fireworks", "model", 8, 0, 2, 10,
                    cost, 25, "success", 200, cache, avoided, path
                )
            rows = await summarize_usage(database_path=path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["requests"], 2)
            self.assertEqual(rows[0]["total_tokens"], 20)
            self.assertEqual(rows[0]["cache_hits"], 1)
            self.assertAlmostEqual(rows[0]["estimated_cost_usd"], 0.01)
            self.assertAlmostEqual(rows[0]["cost_avoided_usd"], 0.01)
            self.assertEqual(rows[1]["requests"], 0)
            self.assertEqual(await summarize_usage(ids[0], path), rows[:1])
            self.assertEqual(await summarize_usage(999, path), [])
            self.assertNotIn("key_hash", rows[0])

    async def test_cli_json_and_filter(self):
        args = build_parser().parse_args(["usage", "--id", "7", "--json"])
        output = io.StringIO()
        with patch("auth.cli.summarize_usage", new_callable=AsyncMock, return_value=[]) as query:
            with contextlib.redirect_stdout(output):
                await run_command(args)
            query.assert_awaited_once_with(7)
        self.assertEqual(json.loads(output.getvalue()), [])

    async def test_cli_table_escapes_labels(self):
        row = dict(virtual_key_id=1, app_name="app\n\x1b[31m", requests=2,
                   total_tokens=20, estimated_cost_usd=0.01, cache_hits=1,
                   cost_avoided_usd=0.01)
        output = io.StringIO()
        with patch("auth.cli.summarize_usage", new_callable=AsyncMock, return_value=[row]):
            with contextlib.redirect_stdout(output):
                await run_command(build_parser().parse_args(["usage"]))
        self.assertIn("0.01000000", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 3)
