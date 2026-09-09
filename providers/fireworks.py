"""Implement the OpenAI-compatible Fireworks provider adapter."""

# Supplies the factory type used to inject mock HTTP clients during tests.
from collections.abc import Callable

# Sends buffered and streamed requests to the Fireworks HTTP API.
import httpx
import anyio

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

    # Sends one request while preserving OpenAI-compatible request and response shapes.
    async def send(self, request: AdapterRequest) -> AdapterResponse:
        # Copies the caller body so replacing its model cannot mutate shared state.
        provider_body = dict(request.body)

        # Replaces the public alias with the trusted real Fireworks model identifier.
        provider_body["model"] = request.upstream_model

        # Builds headers using the real provider key selected after authorization.
        provider_headers = {
            "Authorization": f"Bearer {request.credential.api_key}",
            "Content-Type": "application/json",
        }

        # Selects streaming only for the literal JSON boolean true.
        stream_requested = provider_body.get("stream") is True

        # Requests the final usage-bearing SSE event needed by Phase 5.1 accounting.
        if stream_requested:
            # Reads caller options only when they use the documented object shape.
            stream_options = provider_body.get("stream_options")

            # Copies existing options so the original incoming body remains untouched.
            normalized_stream_options = (
                dict(stream_options)
                if isinstance(stream_options, dict)
                else {}
            )

            # Always includes usage because gateway accounting depends on final totals.
            normalized_stream_options["include_usage"] = True

            # Sends the provider an ordinary OpenAI-compatible stream options object.
            provider_body["stream_options"] = normalized_stream_options

        # Keeps the complete-response behavior used before Phase 3 and Phase 4.
        if not stream_requested:
            # Releases a borrowed pool handle, or closes an independently owned test client.
            async with self.client_factory() as provider_client:
                # Converts network failures into a provider-independent gateway error.
                try:
                    # Sends the translated body and waits for the complete response.
                    provider_response = await provider_client.post(
                        request.credential.url,
                        headers=provider_headers,
                        json=provider_body,
                    )

                # Handles DNS, TLS, connection, and timeout failures from HTTPX.
                except httpx.RequestError as error:
                    # Preserves the original failure as internal diagnostic context.
                    raise ProviderConnectionError from error

            # Returns Fireworks JSON unchanged because it is already OpenAI-compatible.
            return AdapterResponse(
                status_code=provider_response.status_code,
                headers=get_response_headers(provider_response),
                body=provider_response.content,
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
                error_body = await provider_response.aread()
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

        # Returns a lazy body iterator so FastAPI can transmit each SSE chunk immediately.
        return AdapterResponse(
            status_code=provider_response.status_code,
            headers=get_response_headers(provider_response, raw_body=True),
            body=forward_raw_stream(request, provider_response, provider_client),
            streaming=True,
        )
