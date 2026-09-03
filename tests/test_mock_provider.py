"""Verify the cost-free benchmark provider's complete and streaming shapes."""

# Supplies isolated asynchronous unittest support.
import unittest

# Calls the FastAPI mock through ASGI memory transport without opening a port.
import httpx

# Imports the synthetic provider application under test.
from benchmarks.mock_provider import app


# Exercises both response modes used by benchmark and load-test tools.
class MockProviderTests(unittest.IsolatedAsyncioTestCase):
    # Confirms a normal request returns OpenAI-compatible content and usage.
    async def test_complete_response_contains_usage(self) -> None:
        # Routes HTTPX directly into the mock FastAPI application.
        transport = httpx.ASGITransport(app=app)

        # Opens one in-memory asynchronous provider client.
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://mock-provider.test",
        ) as client:
            # Sends the same simple request shape used by real benchmarks.
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "mock-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                },
            )

        # Confirms the mock behaves like a successful OpenAI-compatible provider.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "chat.completion")
        self.assertGreater(response.json()["usage"]["total_tokens"], 0)

    # Confirms streaming emits incremental chunks, usage, and [DONE].
    async def test_streaming_response_contains_usage_and_done(self) -> None:
        # Routes all request bytes through the local ASGI application.
        transport = httpx.ASGITransport(app=app)

        # Opens one in-memory provider client without network access.
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://mock-provider.test",
        ) as client:
            # Requests the synthetic provider's SSE response mode.
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "mock-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )

        # Confirms the payload contains final metrics and the OpenAI terminal marker.
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"usage"', response.content)
        self.assertTrue(response.content.endswith(b"data: [DONE]\n\n"))


# Runs this module directly with normal unittest output.
if __name__ == "__main__":
    # Starts both synthetic provider response tests.
    unittest.main()
