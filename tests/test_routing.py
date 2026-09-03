"""Verify the public endpoint routes models and enforces provider permissions."""

# Encodes route request bodies and parses normalized JSON responses.
import json

# Supplies Python's isolated asynchronous tests and temporary function replacement.
import unittest
from unittest.mock import AsyncMock, patch

# Builds an HTTP-shaped request without starting Uvicorn.
from fastapi import Request

# Imports the real Phase 4 route and its trusted credential registry.
from api import main

# Supplies the response container returned by a provider adapter.
from providers.base import AdapterRequest, AdapterResponse


# Builds one authenticated in-memory request for the real chat route.
def build_chat_request(
    model: str,
    *,
    stream: bool = False,
    extra_body: dict[str, object] | None = None,
) -> Request:
    # Creates the unified JSON body sent by an OpenAI-compatible client.
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": stream,
    }

    # Adds cache controls or other test-specific request parameters when supplied.
    if extra_body is not None:
        # Updates only this isolated request fixture.
        body.update(extra_body)

    # Encodes the complete fixture as the ASGI request body.
    request_body = json.dumps(body).encode("utf-8")

    # Tracks whether Starlette already consumed the request body.
    body_was_sent = False

    # Supplies the one ASGI body event needed by Request.json().
    async def receive() -> dict[str, object]:
        # Allows this closure to update the surrounding delivery state.
        nonlocal body_was_sent

        # Sends the complete request body only once.
        if not body_was_sent:
            # Prevents a second copy from appearing on later receive calls.
            body_was_sent = True

            # Marks this one-event body as complete.
            return {
                "type": "http.request",
                "body": request_body,
                "more_body": False,
            }

        # Represents an open connection with no more request bytes.
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    # Describes the minimum ASGI HTTP information required by Starlette.
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

    # Creates the same request type accepted by the running route.
    request = Request(scope, receive)

    # Mimics safe identity metadata attached by valid virtual-key middleware.
    request.state.virtual_key = {
        "id": 42,
        "app_name": "routing-test-app",
    }

    # Returns the authenticated request without opening a network connection.
    return request


# Records the common AdapterRequest received after routing and authorization.
class RecordingAdapter:
    # Stores the provider identity selected by the test case.
    def __init__(self, name: str) -> None:
        # Matches the name field required by the shared adapter protocol.
        self.name = name

        # Starts empty and receives the request passed by main.py.
        self.received_request: AdapterRequest | None = None

    # Returns one complete response while recording the adapter input.
    async def send(self, request: AdapterRequest) -> AdapterResponse:
        # Saves the routed request for provider, model, and credential assertions.
        self.received_request = request

        # Returns a minimal OpenAI-compatible response body.
        return AdapterResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b'{"ok":true}',
            streaming=False,
        )


# Returns a lazy OpenAI-compatible SSE response for route-level logging tests.
class StreamingRecordingAdapter:
    # Matches the registered provider name selected by the model route.
    name = "fireworks"

    # Returns a body whose usage is unavailable until downstream consumption.
    async def send(self, _request: AdapterRequest) -> AdapterResponse:
        # Produces one usage event and the required completion marker lazily.
        async def stream_body():
            # Sends final cumulative counters in OpenAI's streaming format.
            yield (
                b'data: {"choices":[],"usage":{"prompt_tokens":10,'
                b'"completion_tokens":5,"total_tokens":15}}\n\n'
            )

            # Marks the response stream complete for the client and observer.
            yield b"data: [DONE]\n\n"

        # Gives main.py the same provider-independent streaming container as production.
        return AdapterResponse(
            status_code=200,
            headers={"Content-Type": "text/event-stream"},
            body=stream_body(),
            streaming=True,
        )


# Exercises routing through the real FastAPI route without provider network access.
class MultiProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    # Replaces Phase 5.1 persistence so routing tests never touch the real database.
    async def asyncSetUp(self) -> None:
        # Creates one reusable asynchronous mock for every usage insert in this test.
        self.usage_log_writer = AsyncMock(return_value=1)

        # Replaces only the imported database writer inside the route module.
        self.usage_log_patch = patch.object(
            main,
            "create_usage_log_record",
            self.usage_log_writer,
        )

        # Activates isolation before any route function is called.
        self.usage_log_patch.start()

    # Restores the real writer after each isolated routing test.
    async def asyncTearDown(self) -> None:
        # Stops this test's patch even when an assertion fails.
        self.usage_log_patch.stop()

    # Confirms one endpoint selects Fireworks and Anthropic from the model field.
    async def test_model_selects_provider_adapter(self) -> None:
        # Defines one public model and expected real target for each provider.
        cases = [
            (
                "accounts/fireworks/models/deepseek-v4-flash-0731",
                "fireworks",
                "accounts/fireworks/models/deepseek-v4-flash-0731",
            ),
            (
                "anthropic/claude-sonnet-5",
                "anthropic",
                "claude-sonnet-5",
            ),
        ]

        # Verifies both provider branches through the same route function.
        for public_model, provider, upstream_model in cases:
            # Labels failures with the provider case currently under test.
            with self.subTest(provider=provider):
                # Records the adapter request created by main.py.
                adapter = RecordingAdapter(provider)

                # Returns the provider permission required by this model route.
                permission_lookup = AsyncMock(
                    return_value={
                        "provider": provider,
                        "provider_credential": "default",
                    }
                )

                # Supplies safe test credentials and replaces provider I/O.
                with (
                    patch.dict(
                        main.PROVIDER_CREDENTIALS,
                        {
                            (provider, "default"): {
                                "url": f"https://{provider}.test/api",
                                "api_key": "test-provider-secret",
                            }
                        },
                        clear=True,
                    ),
                    patch.object(
                        main,
                        "get_provider_permission_for_key",
                        permission_lookup,
                    ),
                    patch.object(
                        main,
                        "get_provider_adapter",
                        return_value=adapter,
                    ),
                ):
                    # Calls the real unified endpoint with this public model.
                    response = await main.chat_completions(
                        build_chat_request(public_model)
                    )

                # Confirms the endpoint returned the adapter's successful response.
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.body, b'{"ok":true}')

                # Confirms Phase 5.1 recorded this completed routed request once.
                self.usage_log_writer.assert_awaited_once()

                # Clears this subtest's call before testing the other provider.
                self.usage_log_writer.reset_mock()

                # Confirms authorization checked this key and routed provider together.
                permission_lookup.assert_awaited_once_with(42, provider)

                # Narrows the recorded optional value before examining its fields.
                self.assertIsNotNone(adapter.received_request)
                assert adapter.received_request is not None

                # Confirms public and upstream model identities stayed separate.
                self.assertEqual(
                    adapter.received_request.public_model,
                    public_model,
                )
                self.assertEqual(
                    adapter.received_request.upstream_model,
                    upstream_model,
                )

                # Confirms only trusted server configuration supplied the real secret.
                self.assertEqual(
                    adapter.received_request.credential.api_key,
                    "test-provider-secret",
                )

    # Confirms an unknown model is rejected before authorization or provider work.
    async def test_unknown_model_is_rejected(self) -> None:
        # Tracks whether the database would be consulted unexpectedly.
        permission_lookup = AsyncMock()

        # Replaces the database function only to prove it is not reached.
        with patch.object(
            main,
            "get_provider_permission_for_key",
            permission_lookup,
        ):
            # Calls the route with a model absent from the explicit registry.
            response = await main.chat_completions(
                build_chat_request("unknown/model")
            )

        # Confirms the public model lookup failed with a stable API error.
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.body)["error"]["code"], "model_not_found")

        # Confirms no credential permission or provider request was attempted.
        permission_lookup.assert_not_awaited()

        # Confirms rejected authenticated requests are still measurable.
        self.usage_log_writer.assert_awaited_once()
        self.assertEqual(
            self.usage_log_writer.await_args.kwargs["status"],
            "model_not_found",
        )

    # Confirms a valid key cannot use a provider it was not granted.
    async def test_missing_provider_permission_is_rejected(self) -> None:
        # Simulates a database lookup with no Anthropic permission for this key.
        permission_lookup = AsyncMock(return_value=None)

        # Replaces only the permission query while keeping real model routing.
        with patch.object(
            main,
            "get_provider_permission_for_key",
            permission_lookup,
        ):
            # Requests a configured Anthropic model with an unauthorized key.
            response = await main.chat_completions(
                build_chat_request("anthropic/claude-sonnet-5")
            )

        # Confirms authentication can succeed while authorization returns 403.
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            json.loads(response.body)["error"]["code"],
            "provider_not_allowed",
        )

        # Confirms the permission check used the authenticated key and routed provider.
        permission_lookup.assert_awaited_once_with(42, "anthropic")

        # Confirms authorization denials are stored with their own searchable status.
        self.usage_log_writer.assert_awaited_once()
        self.assertEqual(
            self.usage_log_writer.await_args.kwargs["status"],
            "denied",
        )

    # Confirms stream metrics are written after final usage becomes available.
    async def test_stream_usage_is_logged_after_downstream_consumption(self) -> None:
        # Supplies the Fireworks permission required by the configured public model.
        permission_lookup = AsyncMock(
            return_value={
                "provider": "fireworks",
                "provider_credential": "default",
            }
        )

        # Uses a lazy in-memory adapter that cannot contact a real provider.
        adapter = StreamingRecordingAdapter()

        # Replaces credentials, permission I/O, and provider I/O for this route call.
        with (
            patch.dict(
                main.PROVIDER_CREDENTIALS,
                {
                    ("fireworks", "default"): {
                        "url": "https://fireworks.test/api",
                        "api_key": "test-provider-secret",
                    }
                },
                clear=True,
            ),
            patch.object(
                main,
                "get_provider_permission_for_key",
                permission_lookup,
            ),
            patch.object(
                main,
                "get_provider_adapter",
                return_value=adapter,
            ),
        ):
            # Opens the response without consuming any generated SSE event yet.
            response = await main.chat_completions(
                build_chat_request(
                    "fireworks/deepseek-v4-flash",
                    stream=True,
                )
            )

            # Proves no incomplete row is written when only response headers exist.
            self.usage_log_writer.assert_not_awaited()

            # Consumes the stream as FastAPI would while sending it to a chat client.
            response_chunks = [chunk async for chunk in response.body_iterator]

        # Confirms both provider chunks still reached the downstream consumer.
        self.assertEqual(len(response_chunks), 2)

        # Confirms the final token totals produced exactly one completed usage record.
        self.usage_log_writer.assert_awaited_once()
        log_fields = self.usage_log_writer.await_args.kwargs
        self.assertEqual(log_fields["prompt_tokens"], 10)
        self.assertEqual(log_fields["completion_tokens"], 5)
        self.assertEqual(log_fields["total_tokens"], 15)
        self.assertEqual(log_fields["status"], "success")

    # Confirms an authorized cache hit returns before any provider adapter call.
    async def test_cache_hit_skips_provider_and_logs_avoided_cost(self) -> None:
        # Supplies the provider permission that must still be checked before caching.
        permission_lookup = AsyncMock(
            return_value={
                "provider": "fireworks",
                "provider_credential": "default",
            }
        )

        # Uses a recording adapter so an unexpected provider call is visible.
        adapter = RecordingAdapter("fireworks")

        # Represents one valid unexpired SQLite cache record.
        cached_response = {
            "response_status_code": 200,
            "response_body": b'{"choices":[{"message":{"content":"cached"}}]}',
            "response_headers_json": '{"Content-Type":"application/json"}',
            "estimated_cost_usd": 0.0000042,
        }

        # Replaces all external and database dependencies for this cache-hit request.
        with (
            patch.dict(
                main.PROVIDER_CREDENTIALS,
                {
                    ("fireworks", "default"): {
                        "url": "https://fireworks.test/api",
                        "api_key": "test-provider-secret",
                    }
                },
                clear=True,
            ),
            patch.object(
                main,
                "get_provider_permission_for_key",
                permission_lookup,
            ),
            patch.object(
                main,
                "get_provider_adapter",
                return_value=adapter,
            ),
            patch.object(
                main,
                "read_cached_response",
                AsyncMock(return_value=cached_response),
            ),
        ):
            # Explicitly opts this non-streaming request into response caching.
            response = await main.chat_completions(
                build_chat_request(
                    "fireworks/deepseek-v4-flash",
                    extra_body={"cache": True},
                )
            )

        # Confirms the stored provider response is returned with measurable metadata.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-proxy-cache"], "HIT")
        self.assertEqual(
            response.headers["x-proxy-cost-avoided-usd"],
            "0.0000042",
        )

        # Proves no outbound adapter request was made on the cache hit.
        self.assertIsNone(adapter.received_request)

        # Confirms the hit and avoided cost reached usage logging.
        self.usage_log_writer.assert_awaited_once()
        log_fields = self.usage_log_writer.await_args.kwargs
        self.assertEqual(log_fields["cache_status"], "hit")
        self.assertEqual(log_fields["cost_avoided_usd"], 0.0000042)

    # Confirms a cache miss calls the provider and stores its successful response.
    async def test_cache_miss_calls_provider_and_stores_response(self) -> None:
        # Supplies valid authorization for the deterministic Fireworks request.
        permission_lookup = AsyncMock(
            return_value={
                "provider": "fireworks",
                "provider_credential": "default",
            }
        )

        # Returns one complete response through the normal adapter contract.
        adapter = RecordingAdapter("fireworks")

        # Records whether the new response would be written into SQLite.
        cache_writer = AsyncMock(return_value=True)

        # Simulates an empty cache and prevents all real I/O.
        with (
            patch.dict(
                main.PROVIDER_CREDENTIALS,
                {
                    ("fireworks", "default"): {
                        "url": "https://fireworks.test/api",
                        "api_key": "test-provider-secret",
                    }
                },
                clear=True,
            ),
            patch.object(
                main,
                "get_provider_permission_for_key",
                permission_lookup,
            ),
            patch.object(
                main,
                "get_provider_adapter",
                return_value=adapter,
            ),
            patch.object(
                main,
                "read_cached_response",
                AsyncMock(return_value=None),
            ),
            patch.object(main, "write_cached_response", cache_writer),
        ):
            # Uses temperature zero to select automatic deterministic caching.
            response = await main.chat_completions(
                build_chat_request(
                    "fireworks/deepseek-v4-flash",
                    extra_body={"temperature": 0},
                )
            )

        # Confirms a miss returns the provider response and exposes its source.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-proxy-cache"], "MISS")

        # Confirms the adapter received the request but not a private cache field.
        self.assertIsNotNone(adapter.received_request)
        assert adapter.received_request is not None
        self.assertNotIn("cache", adapter.received_request.body)

        # Confirms the completed provider response was offered to persistent storage.
        cache_writer.assert_awaited_once()

        # Confirms usage reporting classifies the provider call as a miss.
        self.usage_log_writer.assert_awaited_once()
        self.assertEqual(
            self.usage_log_writer.await_args.kwargs["cache_status"],
            "miss",
        )


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest discovery and detailed failure reporting.
    unittest.main()
