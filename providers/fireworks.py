"""Implement the OpenAI-compatible Fireworks provider adapter."""

# Supplies the factory type used to inject mock HTTP clients during tests.
from collections.abc import Callable

# Supplies flexible JSON dictionary annotations used during request preparation.
from typing import Any

# Sends buffered and streamed requests to the Fireworks HTTP API.
import httpx
import anyio

from providers.response_limits import read_bounded_response

# Supplies the shared adapter contract, response container, and network helpers.
from providers.base import (
    AdapterRequest,
    AdapterResponse,
    ProviderConnectionError,
    create_http_client,
    forward_raw_stream,
    get_response_headers,
)


# Adapts the gateway contract to Fireworks' OpenAI-compatible chat endpoint.
class FireworksAdapter:
    # Exposes the stable registry name for this provider implementation.
    name = "fireworks"

    # Stores a replaceable HTTP client factory for production and isolated tests.
    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] = create_http_client,
    ) -> None:
        # Keeps client construction outside request translation and routing logic.
        self.client_factory = client_factory

    def prepare(self, request: AdapterRequest) -> dict[str, Any]:
        from providers.parameters import apply_sampling_policy
        provider_body, _ = apply_sampling_policy(
            request.body, self.name, request.upstream_model, request.drop_sampling_params,
        )
        provider_body["model"] = request.upstream_model
        if provider_body.get("stream") is True:
            options = provider_body.get("stream_options")
            provider_body["stream_options"] = {
                **(options if isinstance(options, dict) else {}),
                "include_usage": True,
            }
        return provider_body

    async def send(self, request: AdapterRequest) -> AdapterResponse:
        provider_body = self.prepare(request)
        provider_headers = {
            "Authorization": f"Bearer {request.credential.api_key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        stream_requested = provider_body.get("stream") is True

        # Keeps the complete-response behavior used before Phase 3 and Phase 4.
        if not stream_requested:
            # Releases a borrowed pool handle, or closes an independently owned test client.
            async with self.client_factory() as provider_client:
                # Converts network failures into a provider-independent gateway error.
                try:
                    # Sends the translated body and waits for the complete response.
                    provider_http_request = provider_client.build_request(
                        "POST", request.credential.url,
                        headers=provider_headers, json=provider_body,
                    )
                    provider_response = await provider_client.send(
                        provider_http_request, stream=True,
                    )
                    try:
                        response_body = await read_bounded_response(provider_response)
                    finally:
                        with anyio.CancelScope(shield=True):
                            await provider_response.aclose()

                # Handles DNS, TLS, connection, and timeout failures from HTTPX.
                except httpx.RequestError as error:
                    # Preserves the original failure as internal diagnostic context.
                    raise ProviderConnectionError from error

            # Returns Fireworks JSON unchanged because it is already OpenAI-compatible.
            return AdapterResponse(
                status_code=provider_response.status_code,
                headers=get_response_headers(provider_response),
                body=response_body,
                streaming=False,
            )

        # Keeps the HTTPX client alive until downstream streaming finishes.
        provider_client = self.client_factory()

        # Opens the upstream stream while converting connection failures consistently.
        try:
            # Builds the provider request separately so HTTPX can avoid reading its body.
            provider_http_request = provider_client.build_request(
                "POST",
                request.credential.url,
                headers=provider_headers,
                json=provider_body,
            )

            # Receives response headers now while leaving the response body unbuffered.
            provider_response = await provider_client.send(
                provider_http_request,
                stream=True,
            )

        # Handles failures that occur before an HTTP response begins.
        except httpx.RequestError as error:
            # Releases the manually managed client because no stream generator owns it.
            await provider_client.aclose()

            # Lets the FastAPI layer create one provider-independent 502 response.
            raise ProviderConnectionError from error

        except BaseException:
            with anyio.CancelScope(shield=True):
                await provider_client.aclose()
            raise

        # Reads error bodies normally because providers do not guarantee SSE for errors.
        if provider_response.is_error:
            # Buffers only the error response so it can be returned as normal JSON.
            try:
                error_body = await read_bounded_response(provider_response)
            except httpx.RequestError as error:
                raise ProviderConnectionError from error
            finally:
                with anyio.CancelScope(shield=True):
                    await provider_response.aclose()
                    await provider_client.aclose()

            # Copies safe headers after HTTPX has decoded the complete error body.
            error_headers = get_response_headers(provider_response)

            # Preserves the existing OpenAI-compatible Fireworks error envelope.
            return AdapterResponse(
                status_code=provider_response.status_code,
                headers=error_headers,
                body=error_body,
                streaming=False,
            )

        async def close_stream():
            with anyio.CancelScope(shield=True):
                await provider_response.aclose()
                await provider_client.aclose()

        # Returns a lazy body iterator so FastAPI can transmit each SSE chunk immediately.
        return AdapterResponse(
            status_code=provider_response.status_code,
            headers=get_response_headers(provider_response, raw_body=True),
            body=forward_raw_stream(request, provider_response, provider_client),
            streaming=True,
            aclose=close_stream,
        )
