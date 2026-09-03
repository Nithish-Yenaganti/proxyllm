"""Verify Phase 5.1 usage extraction, pricing, streaming, and persistence."""

# Supplies asynchronous test support and isolated temporary directories.
import tempfile
import unittest

# Compresses realistic SSE bytes to test transparent stream observation.
import gzip

# Provides exact expected values for per-token cost calculations.
from decimal import Decimal

# Creates an isolated SQLite path for each persistence test.
from pathlib import Path

# Supplies the virtual-key and usage database operations under test.
from auth.database import (
    create_usage_log_record,
    create_virtual_key_record,
    list_usage_log_records,
)

# Supplies deterministic safe key metadata for the foreign-key owner record.
from auth.keys import get_key_prefix, hash_virtual_key

# Supplies all Phase 5.1 response-observation and pricing helpers.
from usage.tracking import (
    OpenAIStreamObserver,
    TokenUsage,
    estimate_cost_usd,
    extract_token_usage,
    observe_stream,
)


# Exercises token normalization and exact request-cost estimation.
class UsageCalculationTests(unittest.TestCase):
    # Confirms complete response fields become provider-independent counters.
    def test_extracts_usage_and_cached_prompt_tokens(self) -> None:
        # Builds the OpenAI-compatible usage shape returned by Fireworks.
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        }

        # Extracts the counters without retaining the provider response body.
        usage = extract_token_usage(payload)

        # Confirms all billable token categories were preserved.
        self.assertEqual(
            usage,
            TokenUsage(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                cached_prompt_tokens=40,
            ),
        )

    # Confirms cached, uncached, and output tokens receive different prices.
    def test_estimates_cost_from_model_rates(self) -> None:
        # Calculates a small Fireworks request using its configured standard rates.
        cost = estimate_cost_usd(
            TokenUsage(100, 20, 120, 40),
            Decimal("0.22"),
            Decimal("0.66"),
            Decimal("0.007"),
        )

        # Confirms the exact per-million-token formula and precision.
        self.assertEqual(cost, Decimal("0.000026680000"))


# Exercises lazy SSE observation without starting FastAPI or a real provider.
class UsageStreamingTests(unittest.IsolatedAsyncioTestCase):
    # Confirms arbitrary chunk boundaries do not affect streaming usage extraction.
    async def test_observes_stream_usage_without_changing_chunks(self) -> None:
        # Builds a final usage event split in the middle of its JSON text.
        chunks = [
            b'data: {"choices":[],"us',
            b'age":{"prompt_tokens":5,"completion_tokens":2,',
            b'"total_tokens":7}}\n\ndata: [DONE]\n\n',
        ]

        # Yields the fixture lazily like an HTTPX provider response.
        async def provider_body():
            # Preserves each original chunk and boundary.
            for chunk in chunks:
                # Makes the bytes available only when the consumer requests them.
                yield chunk

        # Captures facts passed to the completion callback.
        finished = []

        # Records the callback arguments without any database dependency.
        async def on_finished(observation, stream_failed):
            # Saves the immutable end result for assertions.
            finished.append((observation, stream_failed))

        # Creates the same observer used around a real OpenAI-compatible stream.
        observer = OpenAIStreamObserver()

        # Consumes the wrapper and captures every unchanged downstream chunk.
        forwarded = [
            chunk
            async for chunk in observe_stream(
                provider_body(),
                observer,
                on_finished,
            )
        ]

        # Confirms Phase 3 forwarding remains byte-for-byte unchanged.
        self.assertEqual(forwarded, chunks)

        # Confirms the callback ran exactly once after the terminal marker.
        self.assertEqual(len(finished), 1)
        self.assertFalse(finished[0][1])
        self.assertTrue(finished[0][0].completed)
        self.assertEqual(finished[0][0].usage.total_tokens, 7)

    # Confirms gzip observation parses a copy while forwarding compressed bytes.
    async def test_observes_gzip_stream_without_decompressing_client_output(self) -> None:
        # Builds one complete usage-bearing SSE response before compression.
        event_bytes = (
            b'data: {"usage":{"prompt_tokens":3,"completion_tokens":4,'
            b'"total_tokens":7}}\n\ndata: [DONE]\n\n'
        )

        # Compresses the stream exactly as a Content-Encoding provider may do.
        compressed = gzip.compress(event_bytes)

        # Splits compressed bytes to exercise incremental zlib state.
        chunks = [compressed[:8], compressed[8:21], compressed[21:]]

        # Yields the compressed network chunks lazily.
        async def provider_body():
            # Preserves provider boundaries and bytes.
            for chunk in chunks:
                # Sends the next compressed segment.
                yield chunk

        # Captures the final observation through the normal callback contract.
        finished = []

        # Stores callback arguments in memory for verification.
        async def on_finished(observation, stream_failed):
            # Adds the single expected completion result.
            finished.append((observation, stream_failed))

        # Selects incremental gzip decoding for only the observation copy.
        observer = OpenAIStreamObserver("gzip")

        # Consumes the wrapper exactly as StreamingResponse would.
        forwarded = [
            chunk
            async for chunk in observe_stream(
                provider_body(),
                observer,
                on_finished,
            )
        ]

        # Confirms clients still receive the original compressed byte sequence.
        self.assertEqual(b"".join(forwarded), compressed)

        # Confirms metrics were decoded independently from the forwarded content.
        self.assertTrue(finished[0][0].completed)
        self.assertEqual(finished[0][0].usage.prompt_tokens, 3)
        self.assertEqual(finished[0][0].usage.completion_tokens, 4)


# Exercises durable usage rows in a fresh SQLite database.
class UsageDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Creates one isolated key owner and database before each persistence test.
    async def asyncSetUp(self) -> None:
        # Owns a temporary directory that is removed after the test.
        self.temporary_directory = tempfile.TemporaryDirectory()

        # Places test storage outside the real ignored gateway database.
        self.database_path = Path(self.temporary_directory.name) / "usage.db"

        # Uses a deterministic fixture secret that never leaves test memory.
        virtual_key = "nk_phase-five-test_y"

        # Creates the parent key record required by each usage row.
        self.virtual_key_id = await create_virtual_key_record(
            app_name="metrics-app",
            key_prefix=get_key_prefix(virtual_key),
            key_hash=hash_virtual_key(virtual_key),
            provider="fireworks",
            provider_credential="default",
            database_path=self.database_path,
        )

    # Removes every isolated database artifact after each test.
    async def asyncTearDown(self) -> None:
        # Deletes only this test's automatically created temporary directory.
        self.temporary_directory.cleanup()

    # Confirms all required Phase 5.1 fields survive a database round trip.
    async def test_persists_complete_usage_record(self) -> None:
        # Writes one realistic successful provider request accounting row.
        log_id = await create_usage_log_record(
            virtual_key_id=self.virtual_key_id,
            provider="fireworks",
            model="fireworks/deepseek-v4-flash",
            prompt_tokens=100,
            cached_prompt_tokens=40,
            completion_tokens=20,
            total_tokens=120,
            estimated_cost_usd=0.00002668,
            latency_ms=123.45,
            status="success",
            status_code=200,
            database_path=self.database_path,
        )

        # Reads the append-only ledger through its safe database helper.
        records = await list_usage_log_records(self.database_path)

        # Confirms exactly the newly written log exists.
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], log_id)

        # Confirms ownership, routing, usage, cost, latency, and outcome are retained.
        self.assertEqual(records[0]["virtual_key_id"], self.virtual_key_id)
        self.assertEqual(records[0]["provider"], "fireworks")
        self.assertEqual(records[0]["total_tokens"], 120)
        self.assertAlmostEqual(records[0]["estimated_cost_usd"], 0.00002668)
        self.assertEqual(records[0]["latency_ms"], 123.45)
        self.assertEqual(records[0]["status"], "success")
        self.assertEqual(records[0]["status_code"], 200)


# Runs this test module directly with detailed unittest output when requested.
if __name__ == "__main__":
    # Starts normal unittest discovery for this file.
    unittest.main()
