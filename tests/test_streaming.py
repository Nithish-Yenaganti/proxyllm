"""Verify buffered compatibility, SSE passthrough, and stream cleanup."""

# Converts Python request fixtures to the JSON bytes accepted by Starlette.
import json

# Supplies Python's asynchronous test framework and temporary function replacement.
import unittest
from unittest.mock import patch

# Provides mock provider transports, responses, and asynchronous byte streams.
import httpx

# Builds an HTTP-shaped request that can call the route without running Uvicorn.
from fastapi import Request

# Imports the real proxy route and helpers exercised by these tests.
from api import main


# Represents provider bytes while recording whether cleanup closed the stream.
class TrackedByteStream(httpx.AsyncByteStream):
    # Stores the ordered byte chunks returned by the fake provider.
    def __init__(self, chunks: list[bytes]) -> None:
        # Saves a private copy so tests cannot change chunks during iteration.
        self.chunks = list(chunks)

        # Starts false and changes when HTTPX closes the response stream.
        self.was_closed = False

        # Counts consumed chunks so tests can detect accidental route-level buffering.
        self.yield_count = 0

    # Supplies provider chunks to HTTPX asynchronously and in their original order.
    async def __aiter__(self):
        # Visits each configured chunk exactly once.
        for chunk in self.chunks:
            # Records that downstream consumption requested another provider chunk.
            self.yield_count += 1

            # Makes the next provider chunk available without combining the response.
            yield chunk

    # Records that the response stream released its resources.
    async def aclose(self) -> None:
        # Lets assertions prove successful and interrupted streams both clean up.
        self.was_closed = True


# Creates a direct route request containing JSON and authenticated key metadata.
def build_route_request(body: dict[str, object]) -> Request:
    # Encodes the same JSON bytes an OpenAI-compatible client would send over HTTP.
    request_body = json.dumps(body).encode("utf-8")

    # Tracks whether Starlette has already received the one request-body message.
    body_was_sent = False

    # Supplies ASGI messages when Request.json() and disconnect checks ask for them.
    async def receive() -> dict[str, object]:
        # Allows this closure to update the surrounding delivery state.
        nonlocal body_was_sent

        # Sends the complete JSON body on the first receive operation.
        if not body_was_sent:
            # Prevents the same request body from being returned twice.
            body_was_sent = True

            # Marks the body as complete because this fixture uses one ASGI message.
            return {
                "type": "http.request",
                "body": request_body,
                "more_body": False,
            }

        # Represents an open connection with no additional request-body bytes.
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    # Describes the minimum HTTP connection information required by Starlette.
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test-client", 1234),
        "server": ("test-server", 80),
    }

    # Creates the request object consumed by the real chat_completions route.
    request = Request(scope, receive)

    # Mimics the safe authorization record normally attached by middleware.
    request.state.virtual_key = {
        "provider": "fireworks",
        "provider_credential": "default",
    }

    # Returns the prepared request without opening a real network port.
    return request


# Simulates an application that disconnected before the next provider chunk arrived.
class DisconnectedRequest:
    # Reports the client connection state to the stream generator.
    async def is_disconnected(self) -> bool:
        # Forces the generator to stop before forwarding the available chunk.
        return True


# Exercises Phase 3 behavior without contacting Fireworks or reading real credentials.
class StreamingProxyTests(unittest.IsolatedAsyncioTestCase):
    # Confirms compression metadata is copied only with still-encoded raw bytes.
    async def test_content_encoding_matches_the_forwarded_body_mode(self) -> None:
        # Creates provider metadata representing a compressed response body.
        provider_response = httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/event-stream",
                "Content-Encoding": "gzip",
            },
        )

        # Builds headers for HTTPX-decoded normal content.
        buffered_headers = main.get_provider_response_headers(provider_response)

        # Builds headers for raw streaming bytes that remain compressed.
        streaming_headers = main.get_provider_response_headers(
            provider_response,
            raw_body=True,
        )

        # Prevents a normal client from trying to decompress an already-decoded body.
        self.assertNotIn("Content-Encoding", buffered_headers)

        # Tells a streaming client how to decode the unchanged raw provider bytes.
        self.assertEqual(streaming_headers["Content-Encoding"], "gzip")

    # Confirms SSE bytes pass through unchanged and all resources close afterward.
    async def test_streaming_chunks_are_forwarded_and_cleaned_up(self) -> None:
        # Defines realistic SSE events including the OpenAI-style completion marker.
        provider_chunks = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]

        # Tracks closure of the fake provider's response body.
        tracked_stream = TrackedByteStream(provider_chunks)

        # Captures the outbound client so the test can verify final cleanup.
        created_clients: list[httpx.AsyncClient] = []

        # Records the exact JSON body received by the fake provider.
        forwarded_bodies: list[dict[str, object]] = []

        # Handles the proxy's outbound request entirely in memory.
        async def provider_handler(request: httpx.Request) -> httpx.Response:
            # Confirms the proxy replaced the virtual key with the real provider key.
            self.assertEqual(
                request.headers["Authorization"],
                "Bearer test-provider-secret",
            )

            # Saves the parsed outbound body for the unchanged-payload assertion.
            forwarded_bodies.append(json.loads(request.content))

            # Returns an unbuffered SSE response from the fake provider.
            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                },
                stream=tracked_stream,
            )

        # Routes HTTPX requests to provider_handler instead of the internet.
        transport = httpx.MockTransport(provider_handler)

        # Creates the injected outbound client used by the real proxy route.
        def client_factory() -> httpx.AsyncClient:
            # Builds a normal HTTPX client around the in-memory provider transport.
            client = httpx.AsyncClient(transport=transport)

            # Saves the instance so closure can be asserted after consumption.
            created_clients.append(client)

            # Gives the proxy ownership of the client just like production code.
            return client

        # Represents the OpenAI-compatible request sent by a streaming chat app.
        request_body = {
            "model": "accounts/fireworks/models/test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }

        # Uses safe in-memory configuration and replaces only outbound networking.
        with (
            patch.dict(
                main.PROVIDER_CREDENTIALS,
                {
                    ("fireworks", "default"): {
                        "api_key": "test-provider-secret",
                        "url": "https://provider.test/v1/chat/completions",
                    }
                },
                clear=True,
            ),
            patch.object(main, "create_provider_client", side_effect=client_factory),
        ):
            # Calls the real route and receives a StreamingResponse immediately.
            response = await main.chat_completions(build_route_request(request_body))

            # Proves the route returned before reading any provider response-body chunks.
            self.assertEqual(tracked_stream.yield_count, 0)

            # Consumes individual bytes exactly as FastAPI would send them downstream.
            received_chunks = [chunk async for chunk in response.body_iterator]

        # Confirms the route selected an SSE response instead of a normal JSON response.
        self.assertEqual(response.media_type, None)
        self.assertEqual(response.headers["content-type"], "text/event-stream")

        # Confirms the proxy preserved the provider's useful streaming cache header.
        self.assertEqual(response.headers["cache-control"], "no-cache")

        # Confirms no known final size was added to this streaming response.
        self.assertNotIn("content-length", response.headers)

        # Confirms every provider event reached the caller without parsing or rewriting.
        self.assertEqual(received_chunks, provider_chunks)

        # Confirms all three events were pulled only during downstream consumption.
        self.assertEqual(tracked_stream.yield_count, len(provider_chunks))

        # Confirms stream:true and every other request field reached the provider unchanged.
        self.assertEqual(forwarded_bodies, [request_body])

        # Confirms both the provider response and HTTP client released their resources.
        self.assertTrue(tracked_stream.was_closed)
        self.assertTrue(created_clients[0].is_closed)

    # Confirms clients that omit stream:true keep receiving a normal complete response.
    async def test_non_streaming_requests_keep_phase_two_behavior(self) -> None:
        # Captures the client so context-manager cleanup can be checked.
        created_clients: list[httpx.AsyncClient] = []

        # Returns one complete JSON response from the fake provider.
        async def provider_handler(_request: httpx.Request) -> httpx.Response:
            # Mimics the Phase 2 chat-completions response shape.
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": "complete answer"}}]},
            )

        # Routes the outbound request to the fake provider handler.
        transport = httpx.MockTransport(provider_handler)

        # Creates and records the outbound client used by the route.
        def client_factory() -> httpx.AsyncClient:
            # Builds the client with no external network access.
            client = httpx.AsyncClient(transport=transport)

            # Saves it for the cleanup assertion.
            created_clients.append(client)

            # Gives the route its normal client interface.
            return client

        # Omits stream:true so the original buffered response path must run.
        request_body = {
            "model": "accounts/fireworks/models/test-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }

        # Replaces real provider configuration and outbound networking for this test.
        with (
            patch.dict(
                main.PROVIDER_CREDENTIALS,
                {
                    ("fireworks", "default"): {
                        "api_key": "test-provider-secret",
                        "url": "https://provider.test/v1/chat/completions",
                    }
                },
                clear=True,
            ),
            patch.object(main, "create_provider_client", side_effect=client_factory),
        ):
            # Calls the same real route used by streaming requests.
            response = await main.chat_completions(build_route_request(request_body))

        # Confirms the complete provider JSON was returned as one normal body.
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"complete answer", response.body)

        # Confirms the normal context manager still closed its HTTPX client.
        self.assertTrue(created_clients[0].is_closed)

    # Confirms disconnect detection closes upstream resources without forwarding bytes.
    async def test_client_disconnect_closes_upstream_stream(self) -> None:
        # Creates one provider chunk that should never reach a disconnected caller.
        tracked_stream = TrackedByteStream([b"data: should-not-be-sent\n\n"])

        # Associates the response with a request so HTTPX permits raw iteration.
        provider_response = httpx.Response(
            status_code=200,
            request=httpx.Request(
                "POST",
                "https://provider.test/v1/chat/completions",
            ),
            stream=tracked_stream,
        )

        # Creates the manually managed client owned by the stream generator.
        provider_client = httpx.AsyncClient()

        # Runs the real generator with a caller that reports an immediate disconnect.
        received_chunks = [
            chunk
            async for chunk in main.stream_provider_body(
                DisconnectedRequest(),
                provider_response,
                provider_client,
            )
        ]

        # Confirms no provider data was sent after the disconnect was detected.
        self.assertEqual(received_chunks, [])

        # Confirms the response and client both closed despite stopping early.
        self.assertTrue(tracked_stream.was_closed)
        self.assertTrue(provider_client.is_closed)


# Runs this test file directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest discovery and detailed failure reporting for this module.
    unittest.main()
