"""Verify Anthropic translation and explicit multi-provider model routing."""

# Parses outbound request bodies and normalized response or SSE data.
import json

# Supplies Python's isolated asynchronous test framework.
import unittest

# Provides in-memory HTTP transports and streamed provider responses.
import httpx

# Supplies Anthropic translation and its concrete adapter implementation.
from providers.anthropic import AnthropicAdapter, translate_anthropic_request

# Supplies the shared request, credential, and request-error types.
from providers.base import AdapterRequest, ProviderCredential, ProviderRequestError

# Supplies the explicit public-model routing registry.
from providers.registry import get_model_route, get_provider_adapter


# Represents Anthropic SSE bytes while recording whether cleanup ran.
class AnthropicEventStream(httpx.AsyncByteStream):
    # Stores provider chunks and initializes observable closure state.
    def __init__(self, chunks: list[bytes]) -> None:
        # Copies the event chunks so their order remains stable during testing.
        self.chunks = list(chunks)

        # Starts false and changes when the adapter closes the provider response.
        self.was_closed = False

    # Supplies each provider event chunk asynchronously.
    async def __aiter__(self):
        # Visits every configured chunk exactly once.
        for chunk in self.chunks:
            # Gives HTTPX the next piece of the Anthropic SSE response.
            yield chunk

    # Records release of the upstream stream.
    async def aclose(self) -> None:
        # Makes cleanup visible to assertions.
        self.was_closed = True


# Represents a downstream application that remains connected.
async def client_is_connected() -> bool:
    # Reports false so adapters continue producing response data.
    return False


# Represents a downstream application that has already closed its connection.
async def client_is_disconnected() -> bool:
    # Reports true so the Anthropic translator stops before emitting another event.
    return True


# Creates one safe adapter request shared by translation tests.
def build_anthropic_request(
    body: dict[str, object],
) -> AdapterRequest:
    # Packages a public alias, real provider model, and fake secret without networking.
    return AdapterRequest(
        body=body,
        public_model="anthropic/claude-sonnet-5",
        upstream_model="claude-sonnet-5",
        credential=ProviderCredential(
            url="https://api.anthropic.test/v1/messages",
            api_key="test-anthropic-secret",
        ),
        is_disconnected=client_is_connected,
    )


# Exercises provider routing and both Anthropic response modes.
class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    # Confirms explicit model names select the correct providers and upstream IDs.
    async def test_model_registry_routes_without_guessing(self) -> None:
        # Resolves the existing Fireworks model used before Phase 4.
        fireworks_route = get_model_route(
            "accounts/fireworks/models/deepseek-v4-flash-0731"
        )

        # Resolves the new Anthropic provider-qualified model alias.
        anthropic_route = get_model_route("anthropic/claude-sonnet-5")

        # Confirms the two models select independent provider adapters.
        self.assertEqual(fireworks_route.provider, "fireworks")
        self.assertEqual(anthropic_route.provider, "anthropic")

        # Confirms the Anthropic alias maps to its trusted provider model ID.
        self.assertEqual(anthropic_route.upstream_model, "claude-sonnet-5")

        # Confirms unknown model names are rejected instead of prefix-guessed.
        self.assertIsNone(get_model_route("claude-made-up-model"))

        # Confirms both routed provider implementations satisfy the registry lookup.
        self.assertEqual(get_provider_adapter("fireworks").name, "fireworks")
        self.assertEqual(get_provider_adapter("anthropic").name, "anthropic")

    # Confirms OpenAI system messages and common fields translate to Anthropic.
    async def test_anthropic_request_translation(self) -> None:
        # Builds a representative unified text-chat request.
        adapter_request = build_anthropic_request(
            {
                "model": "anthropic/claude-sonnet-5",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "developer", "content": "Use plain language."},
                    {"role": "user", "content": "Hello"},
                ],
                "max_tokens": 150,
                "stop": "END",
                "stream": False,
            }
        )

        # Runs only the deterministic request translator.
        provider_body = translate_anthropic_request(adapter_request)

        # Confirms routing replaced the public model with the real Anthropic model.
        self.assertEqual(provider_body["model"], "claude-sonnet-5")

        # Confirms instruction roles moved to Anthropic's top-level system field.
        self.assertEqual(
            provider_body["system"],
            "Be concise.\n\nUse plain language.",
        )

        # Confirms only conversational roles remain inside messages.
        self.assertEqual(
            provider_body["messages"],
            [{"role": "user", "content": "Hello"}],
        )

        # Confirms common generation fields were translated explicitly.
        self.assertEqual(provider_body["max_tokens"], 150)
        self.assertEqual(provider_body["stop_sequences"], ["END"])
        self.assertFalse(provider_body["stream"])

    # Confirms unsupported capabilities fail clearly instead of losing information.
    async def test_anthropic_translation_rejects_unsupported_tools(self) -> None:
        # Creates an OpenAI tool request not yet covered by the text-chat adapter.
        adapter_request = build_anthropic_request(
            {
                "model": "anthropic/claude-sonnet-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "tools": [{"type": "function", "function": {"name": "demo"}}],
            }
        )

        # Confirms the adapter refuses a lossy or misleading translation.
        with self.assertRaises(ProviderRequestError):
            # Runs the deterministic translator without contacting a provider.
            translate_anthropic_request(adapter_request)

    # Confirms a complete Anthropic Message becomes an OpenAI chat completion.
    async def test_anthropic_non_streaming_response_is_normalized(self) -> None:
        # Records the translated provider request for header and body assertions.
        captured_requests: list[httpx.Request] = []

        # Handles the adapter's outbound Anthropic call in memory.
        async def provider_handler(request: httpx.Request) -> httpx.Response:
            # Saves the complete request for assertions after adapter execution.
            captured_requests.append(request)

            # Returns a documented Anthropic Message-shaped response.
            return httpx.Response(
                200,
                json={
                    "id": "msg_test_123",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": " from Claude"},
                    ],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                },
            )

        # Creates an Anthropic adapter that cannot use the real network.
        adapter = AnthropicAdapter(
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(provider_handler)
            )
        )

        # Sends one ordinary non-streaming unified request.
        adapter_response = await adapter.send(
            build_anthropic_request(
                {
                    "model": "anthropic/claude-sonnet-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 100,
                }
            )
        )

        # Parses the OpenAI-compatible response created by the adapter.
        normalized_body = json.loads(adapter_response.body)

        # Confirms direct Anthropic authentication and API versioning headers.
        self.assertEqual(
            captured_requests[0].headers["x-api-key"],
            "test-anthropic-secret",
        )
        self.assertEqual(
            captured_requests[0].headers["anthropic-version"],
            "2023-06-01",
        )

        # Confirms provider content blocks became one assistant message string.
        self.assertEqual(
            normalized_body["choices"][0]["message"],
            {"role": "assistant", "content": "Hello from Claude"},
        )

        # Confirms finish reason, public model alias, and usage were normalized.
        self.assertEqual(normalized_body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(normalized_body["model"], "anthropic/claude-sonnet-5")
        self.assertEqual(
            normalized_body["usage"],
            {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
            },
        )
        self.assertFalse(adapter_response.streaming)

    # Confirms Anthropic named events become incremental OpenAI-compatible SSE.
    async def test_anthropic_stream_is_normalized_and_cleaned_up(self) -> None:
        # Defines the documented Anthropic event sequence across separate chunks.
        event_stream = AnthropicEventStream(
            [
                b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_stream_1","usage":{"input_tokens":5,"output_tokens":0}}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":" world"}}\n\n',
                b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
        )

        # Captures the manually managed HTTPX client for cleanup verification.
        created_clients: list[httpx.AsyncClient] = []

        # Returns the fake Anthropic SSE response.
        async def provider_handler(_request: httpx.Request) -> httpx.Response:
            # Associates the event stream with the expected provider content type.
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=event_stream,
            )

        # Creates and records the adapter's injected HTTP client.
        def client_factory() -> httpx.AsyncClient:
            # Builds a client backed only by the in-memory handler.
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(provider_handler)
            )

            # Saves it so closure can be asserted after event translation.
            created_clients.append(client)

            # Gives the adapter ownership of this client.
            return client

        # Creates the translating provider implementation.
        adapter = AnthropicAdapter(client_factory=client_factory)

        # Opens a streaming Anthropic request through the shared adapter contract.
        adapter_response = await adapter.send(
            build_anthropic_request(
                {
                    "model": "anthropic/claude-sonnet-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                    "max_tokens": 100,
                }
            )
        )

        # Consumes the normalized chunks as FastAPI's StreamingResponse will do.
        normalized_chunks = [chunk async for chunk in adapter_response.body]

        # Confirms the response remains an SSE stream for existing chat clients.
        self.assertTrue(adapter_response.streaming)
        self.assertEqual(
            adapter_response.headers["Content-Type"],
            "text/event-stream",
        )

        # Extracts each normalized OpenAI data event except the completion marker.
        normalized_events = [
            json.loads(chunk.removeprefix(b"data: ").strip())
            for chunk in normalized_chunks
            if chunk != b"data: [DONE]\n\n"
        ]

        # Confirms the initial event establishes the assistant role.
        self.assertEqual(
            normalized_events[0]["choices"][0]["delta"],
            {"role": "assistant", "content": ""},
        )

        # Confirms token fragments remain separate and ordered for live rendering.
        self.assertEqual(
            [
                normalized_events[1]["choices"][0]["delta"]["content"],
                normalized_events[2]["choices"][0]["delta"]["content"],
            ],
            ["Hello", " world"],
        )

        # Confirms the terminal event contains normalized reason and usage.
        self.assertEqual(
            normalized_events[-1]["choices"][0]["finish_reason"],
            "stop",
        )
        self.assertEqual(
            normalized_events[-1]["usage"],
            {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        )

        # Confirms the adapter adds the marker Anthropic itself does not send.
        self.assertEqual(normalized_chunks[-1], b"data: [DONE]\n\n")

        # Confirms normal completion releases both upstream resources.
        self.assertTrue(event_stream.was_closed)
        self.assertTrue(created_clients[0].is_closed)

    # Confirms Anthropic streaming also preserves Phase 3 disconnect cleanup.
    async def test_anthropic_disconnect_closes_upstream_stream(self) -> None:
        # Creates one valid provider event that must not reach a disconnected caller.
        event_stream = AnthropicEventStream(
            [
                b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_disconnected","usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
            ]
        )

        # Returns the unbuffered event stream entirely in memory.
        async def provider_handler(_request: httpx.Request) -> httpx.Response:
            # Associates the provider bytes with Anthropic's SSE content type.
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=event_stream,
            )

        # Captures the adapter's manually managed client.
        provider_client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider_handler)
        )

        # Creates the Anthropic adapter around the known test client.
        adapter = AnthropicAdapter(client_factory=lambda: provider_client)

        # Builds a streaming request whose downstream connection is already closed.
        adapter_request = AdapterRequest(
            body={
                "model": "anthropic/claude-sonnet-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            public_model="anthropic/claude-sonnet-5",
            upstream_model="claude-sonnet-5",
            credential=ProviderCredential(
                url="https://api.anthropic.test/v1/messages",
                api_key="test-anthropic-secret",
            ),
            is_disconnected=client_is_disconnected,
        )

        # Opens the stream and then lets the translator observe the disconnect.
        adapter_response = await adapter.send(adapter_request)
        received_chunks = [chunk async for chunk in adapter_response.body]

        # Confirms no normalized data was emitted after the client disconnected.
        self.assertEqual(received_chunks, [])

        # Confirms both Anthropic upstream resources were released immediately.
        self.assertTrue(event_stream.was_closed)
        self.assertTrue(provider_client.is_closed)

    # Confirms Anthropic errors become the same outer error shape clients already use.
    async def test_anthropic_error_is_normalized(self) -> None:
        # Returns one documented provider error envelope.
        async def provider_handler(_request: httpx.Request) -> httpx.Response:
            # Mimics a provider-side request validation failure.
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Invalid max_tokens value.",
                    },
                },
            )

        # Creates the adapter with an in-memory error response.
        adapter = AnthropicAdapter(
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(provider_handler)
            )
        )

        # Sends a valid gateway request that the fake provider rejects.
        adapter_response = await adapter.send(
            build_anthropic_request(
                {
                    "model": "anthropic/claude-sonnet-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            )
        )

        # Parses the provider-independent error body returned to the client.
        normalized_error = json.loads(adapter_response.body)

        # Confirms both HTTP status and useful provider message were preserved.
        self.assertEqual(adapter_response.status_code, 400)
        self.assertEqual(
            normalized_error["error"]["message"],
            "Invalid max_tokens value.",
        )
        self.assertEqual(
            normalized_error["error"]["code"],
            "invalid_request_error",
        )


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest discovery and detailed failure reporting.
    unittest.main()
