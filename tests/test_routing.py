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
def build_chat_request(model: str) -> Request:
    # Creates the unified JSON body sent by an OpenAI-compatible client.
    request_body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Hello"}],
        }
    ).encode("utf-8")

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


# Exercises routing through the real FastAPI route without provider network access.
class MultiProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
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


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest discovery and detailed failure reporting.
    unittest.main()
