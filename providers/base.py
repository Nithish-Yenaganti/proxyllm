"""Define the shared request, response, errors, and HTTP helpers for adapters."""

# Supplies asynchronous iterator and callback types used by streamed responses.
from collections.abc import AsyncIterator, Awaitable, Callable

# Supplies immutable data containers for adapter inputs, outputs, and credentials.
from dataclasses import dataclass

# Supplies flexible JSON-compatible annotations for provider request dictionaries.
from typing import Any, Protocol

# Sends asynchronous provider requests and exposes unbuffered response bodies.
import httpx


# Describes the asynchronous check used to stop work after a client disconnects.
DisconnectCheck = Callable[[], Awaitable[bool]]


# Identifies provider connection failures that should become gateway HTTP 502 errors.
class ProviderConnectionError(Exception):
    """Raised when an adapter cannot communicate with its upstream provider."""


# Identifies unified requests that cannot be translated safely for one provider.
class ProviderRequestError(Exception):
    """Raised when an adapter cannot translate the supplied gateway request."""


# Holds trusted server-side configuration selected after authorization succeeds.
@dataclass(frozen=True)
class ProviderCredential:
    # Stores the provider endpoint that receives this request.
    url: str

    # Stores the real provider secret that is never returned to the calling app.
    api_key: str


# Holds everything an adapter needs to perform one normalized gateway request.
@dataclass(frozen=True)
class AdapterRequest:
    # Contains the OpenAI-compatible JSON body received by the public endpoint.
    body: dict[str, Any]

    # Preserves the public model name expected in normalized responses.
    public_model: str

    # Selects the real provider model without trusting arbitrary provider input.
    upstream_model: str

    # Supplies the already-authorized real endpoint and provider secret.
    credential: ProviderCredential

    # Lets a streaming adapter stop promptly when the downstream app disconnects.
    is_disconnected: DisconnectCheck


# Holds either one complete response body or one asynchronous stream of response bytes.
@dataclass(frozen=True)
class AdapterResponse:
    # Preserves the provider status code or the normalized equivalent.
    status_code: int

    # Contains only safe response headers that should cross the gateway boundary.
    headers: dict[str, str]

    # Contains complete bytes for normal responses or an iterator for streaming ones.
    body: bytes | AsyncIterator[bytes]

    # Tells FastAPI whether to use Response or StreamingResponse.
    streaming: bool


# Defines the one common function signature implemented by every provider module.
class ProviderAdapter(Protocol):
    # Names the provider so the registry and diagnostics can identify the adapter.
    name: str

    # Sends one authorized request and returns a unified response container.
    async def send(self, request: AdapterRequest) -> AdapterResponse:
        """Translate, send, and normalize one provider request."""


# Creates the normal outbound HTTPX client used by production adapters.
def create_http_client() -> httpx.AsyncClient:
    # Limits each connection phase and the wait between streamed events to 60 seconds.
    return httpx.AsyncClient(timeout=60.0)


# Selects headers that remain correct after a provider response crosses the gateway.
def get_response_headers(
    provider_response: httpx.Response,
    *,
    raw_body: bool = False,
) -> dict[str, str]:
    # Always tells the downstream client how to interpret the response body.
    response_headers = {
        "Content-Type": provider_response.headers.get(
            "content-type",
            "application/json",
        )
    }

    # Reads optional cache behavior commonly supplied with SSE responses.
    cache_control = provider_response.headers.get("cache-control")

    # Copies cache behavior only when the upstream provider supplied it.
    if cache_control is not None:
        # Preserves provider cache semantics without copying connection headers.
        response_headers["Cache-Control"] = cache_control

    # Raw streaming bytes may still require the provider's content decoder.
    if raw_body:
        # Reads optional compression metadata from the upstream response.
        content_encoding = provider_response.headers.get("content-encoding")

        # Adds compression metadata only when the raw body actually uses it.
        if content_encoding is not None:
            # Keeps the raw bytes and their decoding instructions consistent.
            response_headers["Content-Encoding"] = content_encoding

    # Excludes Content-Length, Transfer-Encoding, and other hop-by-hop headers.
    return response_headers


# Passes an OpenAI-compatible provider stream through without parsing its SSE events.
async def forward_raw_stream(
    request: AdapterRequest,
    provider_response: httpx.Response,
    provider_client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    # Guarantees network cleanup after success, failure, cancellation, or disconnect.
    try:
        # Pulls one available upstream byte chunk at a time without full buffering.
        async for chunk in provider_response.aiter_raw():
            # Stops forwarding when the calling application has closed its connection.
            if await request.is_disconnected():
                # Leaves the loop so the finally block closes the provider connection.
                break

            # Sends the unchanged provider bytes toward the client immediately.
            yield chunk

    # Runs even when FastAPI cancels iteration during a downstream disconnect.
    finally:
        # Releases the upstream response and its network connection.
        await provider_response.aclose()

        # Releases the manually managed HTTPX client that owns the stream.
        await provider_client.aclose()
