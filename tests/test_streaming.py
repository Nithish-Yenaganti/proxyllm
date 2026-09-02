"""Verify Phase 3 Fireworks streaming still works behind its Phase 4 adapter."""

# Parses the outbound provider request body for unchanged-payload assertions.
import json

# Supplies Python's isolated asynchronous test framework.
import unittest

# Provides mock HTTP transports, responses, and asynchronous byte streams.
import httpx

# Supplies the shared adapter request, credential, and stream helpers.
from providers.base import AdapterRequest, ProviderCredential, get_response_headers

# Supplies the OpenAI-compatible provider adapter under test.
from providers.fireworks import FireworksAdapter


# Represents provider bytes while recording consumption and cleanup.
class TrackedByteStream(httpx.AsyncByteStream):
    # Stores ordered chunks and initializes observable lifecycle state.
    def __init__(self, chunks: list[bytes]) -> None:
        # Copies the chunk list so external mutation cannot affect iteration.
        self.chunks = list(chunks)

        # Starts false and changes when HTTPX closes the response stream.
        self.was_closed = False

        # Counts consumed chunks to detect accidental buffering inside send().
        self.yield_count = 0

    # Supplies one provider chunk at a time in its original order.
    async def __aiter__(self):
        # Visits every configured chunk exactly once.
        for chunk in self.chunks:
            # Records downstream demand for this provider chunk.
            self.yield_count += 1

            # Gives the raw bytes to HTTPX without joining the complete response.
            yield chunk

    # Records that the provider response released its stream.
    async def aclose(self) -> None:
        # Makes cleanup observable to test assertions.
        self.was_closed = True


# Reports a configurable downstream connection state to adapter stream helpers.
class DisconnectState:
    # Stores whether the simulated client connection has ended.
    def __init__(self, disconnected: bool = False) -> None:
        # Saves the state returned by each asynchronous check.
        self.disconnected = disconnected

    # Matches the callback shape supplied by FastAPI Request.is_disconnected.
    async def check(self) -> bool:
        # Returns the current simulated connection state.
        return self.disconnected


# Exercises Fireworks streaming without contacting a real provider.
class FireworksStreamingTests(unittest.IsolatedAsyncioTestCase):
    # Confirms compression metadata follows only raw, still-encoded response bodies.
    async def test_content_encoding_matches_forwarded_body_mode(self) -> None:
        # Creates metadata representing a compressed upstream SSE response.
        provider_response = httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/event-stream",
                "Content-Encoding": "gzip",
            },
        )

        # Builds headers for decoded normal content.
        buffered_headers = get_response_headers(provider_response)

        # Builds headers for unchanged raw provider bytes.
        streaming_headers = get_response_headers(provider_response, raw_body=True)

        # Prevents clients from decoding a buffered body twice.
        self.assertNotIn("Content-Encoding", buffered_headers)

        # Preserves decoding instructions for raw streamed bytes.
        self.assertEqual(streaming_headers["Content-Encoding"], "gzip")

    # Confirms SSE chunks pass through lazily, unchanged, and with complete cleanup.
    async def test_streaming_chunks_are_forwarded_and_cleaned_up(self) -> None:
        # Defines realistic OpenAI-compatible SSE events and completion marker.
        provider_chunks = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]

        # Tracks consumption and closure of the fake provider body.
        tracked_stream = TrackedByteStream(provider_chunks)

        # Captures the client so final cleanup can be asserted.
        created_clients: list[httpx.AsyncClient] = []

        # Records the exact translated provider body.
        forwarded_bodies: list[dict[str, object]] = []

        # Handles the adapter's outbound call entirely in memory.
        async def provider_handler(request: httpx.Request) -> httpx.Response:
            # Confirms the adapter used its authorized real provider credential.
            self.assertEqual(
                request.headers["Authorization"],
                "Bearer test-provider-secret",
            )

            # Records the decoded outbound JSON for model and payload assertions.
            forwarded_bodies.append(json.loads(request.content))

            # Returns an unbuffered provider response with standard SSE headers.
            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                },
                stream=tracked_stream,
            )

        # Routes HTTPX calls to the in-memory provider handler.
        transport = httpx.MockTransport(provider_handler)

        # Creates the injected adapter client and records its lifecycle.
        def client_factory() -> httpx.AsyncClient:
            # Builds a normal HTTPX client that cannot reach the internet.
            client = httpx.AsyncClient(transport=transport)

            # Saves the client for its final is_closed assertion.
            created_clients.append(client)

            # Gives the adapter ownership of this client.
            return client

        # Creates the OpenAI-compatible adapter implementation under test.
        adapter = FireworksAdapter(client_factory=client_factory)

        # Simulates a connected downstream chat client.
        disconnect_state = DisconnectState()

        # Builds the provider-independent request passed by routing code.
        adapter_request = AdapterRequest(
            body={
                "model": "fireworks/public-alias",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            public_model="fireworks/public-alias",
            upstream_model="accounts/fireworks/models/test-model",
            credential=ProviderCredential(
                url="https://provider.test/v1/chat/completions",
                api_key="test-provider-secret",
            ),
            is_disconnected=disconnect_state.check,
        )

        # Opens the provider response and receives a lazy body iterator.
        adapter_response = await adapter.send(adapter_request)

        # Proves send() returned before consuming any generation chunk.
        self.assertEqual(tracked_stream.yield_count, 0)

        # Consumes chunks the same way FastAPI's StreamingResponse will consume them.
        received_chunks = [chunk async for chunk in adapter_response.body]

        # Confirms the adapter selected streaming with the expected safe headers.
        self.assertTrue(adapter_response.streaming)
        self.assertEqual(
            adapter_response.headers["Content-Type"],
            "text/event-stream",
        )
        self.assertNotIn("Content-Length", adapter_response.headers)

        # Confirms every event and data byte crossed the adapter unchanged.
        self.assertEqual(received_chunks, provider_chunks)
        self.assertEqual(tracked_stream.yield_count, len(provider_chunks))

        # Confirms only the trusted upstream model replaced the public alias.
        self.assertEqual(
            forwarded_bodies,
            [
                {
                    "model": "accounts/fireworks/models/test-model",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                }
            ],
        )

        # Confirms normal stream completion released both HTTPX resources.
        self.assertTrue(tracked_stream.was_closed)
        self.assertTrue(created_clients[0].is_closed)

    # Confirms a downstream disconnect stops forwarding and closes upstream resources.
    async def test_client_disconnect_closes_upstream_stream(self) -> None:
        # Creates one chunk that must not reach an already-disconnected caller.
        tracked_stream = TrackedByteStream([b"data: should-not-be-sent\n\n"])

        # Returns that lazy stream from an in-memory provider.
        async def provider_handler(_request: httpx.Request) -> httpx.Response:
            # Associates standard SSE metadata with the provider stream.
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=tracked_stream,
            )

        # Prevents any real outbound network operation.
        transport = httpx.MockTransport(provider_handler)

        # Captures the manually managed provider client.
        provider_client = httpx.AsyncClient(transport=transport)

        # Creates an adapter that returns that known client.
        adapter = FireworksAdapter(client_factory=lambda: provider_client)

        # Simulates a client that disconnects before the first chunk is delivered.
        disconnect_state = DisconnectState(disconnected=True)

        # Builds one valid streaming adapter request.
        adapter_request = AdapterRequest(
            body={"model": "alias", "messages": [], "stream": True},
            public_model="alias",
            upstream_model="upstream-model",
            credential=ProviderCredential(
                url="https://provider.test/v1/chat/completions",
                api_key="secret",
            ),
            is_disconnected=disconnect_state.check,
        )

        # Opens the lazy provider stream.
        adapter_response = await adapter.send(adapter_request)

        # Attempts to consume output after the client is already disconnected.
        received_chunks = [chunk async for chunk in adapter_response.body]

        # Confirms nothing was forwarded after disconnect detection.
        self.assertEqual(received_chunks, [])

        # Confirms both upstream resources closed despite the early exit.
        self.assertTrue(tracked_stream.was_closed)
        self.assertTrue(provider_client.is_closed)

    # Confirms requests without stream:true retain their complete response behavior.
    async def test_non_streaming_requests_remain_supported(self) -> None:
        # Returns one complete OpenAI-compatible JSON response.
        async def provider_handler(_request: httpx.Request) -> httpx.Response:
            # Mimics a successful complete Fireworks response.
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "complete answer"}}]},
            )

        # Creates an in-memory client for the normal adapter path.
        adapter = FireworksAdapter(
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(provider_handler)
            )
        )

        # Builds a request with no streaming flag.
        adapter_request = AdapterRequest(
            body={"model": "alias", "messages": []},
            public_model="alias",
            upstream_model="upstream-model",
            credential=ProviderCredential(
                url="https://provider.test/v1/chat/completions",
                api_key="secret",
            ),
            is_disconnected=DisconnectState().check,
        )

        # Waits for the complete provider response.
        adapter_response = await adapter.send(adapter_request)

        # Confirms Phase 2 complete-response semantics still apply.
        self.assertFalse(adapter_response.streaming)
        self.assertIn(b"complete answer", adapter_response.body)


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest discovery and detailed failure reporting.
    unittest.main()
