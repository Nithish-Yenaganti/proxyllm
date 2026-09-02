"""Run the authenticated OpenAI-compatible ProxyLLM HTTP API."""

# Supplies the asynchronous iterator type used by the streaming response body.
from collections.abc import AsyncIterator

# Provides an asynchronous startup and shutdown context for FastAPI.
from contextlib import asynccontextmanager

# Reads provider credentials and URLs from the process environment.
import os

# Sends non-blocking outbound HTTP requests to the selected provider.
import httpx

# Loads local development variables from the ignored .env file.
from dotenv import load_dotenv

# Provides the web application, incoming request, and outgoing response types.
from fastapi import FastAPI, Request, Response

# Provides structured JSON errors and unbuffered streaming HTTP responses.
from fastapi.responses import JSONResponse, StreamingResponse

# Protects /v1/* routes with active virtual API keys.
from api.middleware import VirtualKeyAuthMiddleware

# Ensures the authentication database exists when the server starts.
from auth.database import initialize_database


# Loads .env values without displaying or logging their contents.
load_dotenv()


# Maps a permitted database reference to the real server-side Fireworks settings.
PROVIDER_CREDENTIALS = {
    # Allows records assigned to fireworks/default to use this environment credential.
    ("fireworks", "default"): {
        # Reads the secret that will authenticate the outbound provider request.
        "api_key": os.getenv("FIREWORK_API_KEY"),
        # Reads the provider's chat-completions endpoint.
        "url": os.getenv("FIREWORK_URL"),
    }
}


# Creates one outbound HTTP client with a maximum wait between provider events.
def create_provider_client() -> httpx.AsyncClient:
    # A separate function lets automated tests replace the network transport safely.
    return httpx.AsyncClient(timeout=60.0)


# Builds the trusted headers sent to the selected real provider.
def build_provider_headers(provider_api_key: str) -> dict[str, str]:
    # Replaces the caller's virtual key with the server-side provider credential.
    return {
        "Authorization": f"Bearer {provider_api_key}",
        "Content-Type": "application/json",
    }


# Creates one stable gateway error when the provider connection cannot be opened.
def provider_connection_error() -> JSONResponse:
    # Uses 502 because the proxy failed while communicating with an upstream server.
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "message": "The upstream provider could not be reached.",
                "type": "provider_connection_error",
                "code": "provider_unavailable",
            }
        },
    )


# Selects safe provider response headers that remain meaningful through the proxy.
def get_provider_response_headers(
    provider_response: httpx.Response,
    *,
    raw_body: bool = False,
) -> dict[str, str]:
    # Always gives the client the provider's content type when one is available.
    response_headers = {
        "Content-Type": provider_response.headers.get(
            "content-type",
            "application/json",
        )
    }

    # Preserves cache instructions commonly included with SSE responses.
    cache_control = provider_response.headers.get("cache-control")

    # Adds Cache-Control only when the provider actually supplied it.
    if cache_control is not None:
        # Copies the provider value without exposing any credentials.
        response_headers["Cache-Control"] = cache_control

    # Preserves compression metadata only when raw, still-encoded bytes are forwarded.
    if raw_body:
        # Reads the encoding that the downstream client must apply to those raw bytes.
        content_encoding = provider_response.headers.get("content-encoding")

        # Adds Content-Encoding only when the provider actually compressed the body.
        if content_encoding is not None:
            # Keeps the raw response body and its decoding instructions consistent.
            response_headers["Content-Encoding"] = content_encoding

    # Deliberately excludes Content-Length and other connection-specific headers.
    return response_headers


# Yields provider bytes immediately and owns cleanup for the whole stream lifetime.
async def stream_provider_body(
    request: Request,
    provider_response: httpx.Response,
    provider_client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    # Guarantees cleanup after success, provider failure, cancellation, or disconnect.
    try:
        # Reads each available upstream byte chunk without collecting the full answer.
        async for chunk in provider_response.aiter_raw():
            # Stops paying for and processing output when the calling app disconnects.
            if await request.is_disconnected():
                # Exits the loop so the finally block closes the upstream connection.
                break

            # Gives this chunk to FastAPI immediately for delivery to the caller.
            yield chunk

    # Runs even when Starlette cancels this generator after a client disconnect.
    finally:
        # Releases the provider response and its underlying network connection.
        await provider_response.aclose()

        # Releases the HTTPX client after its streamed response is finished.
        await provider_client.aclose()


# Performs one-time asynchronous application startup work.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Creates the SQLite file and virtual_keys table when they are missing.
    await initialize_database()

    # Hands control to FastAPI for the lifetime of the running server.
    yield


# Creates the Uvicorn-served FastAPI application with database startup enabled.
app = FastAPI(lifespan=lifespan)

# Applies virtual-key authentication before requests reach protected routes.
app.add_middleware(VirtualKeyAuthMiddleware)


# Registers a public route that confirms the process is reachable.
@app.get("/")
def read_root():
    # Returns a small JSON health response without requiring authentication.
    return {"message": "Hello, World!"}


# Accepts the OpenAI-compatible chat-completions path used by client applications.
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Reads safe metadata attached by the successful authentication middleware.
    virtual_key_record = request.state.virtual_key

    # Builds the exact provider permission reference stored for this virtual key.
    provider_reference = (
        str(virtual_key_record["provider"]),
        str(virtual_key_record["provider_credential"]),
    )

    # Resolves the permitted server-side credential without trusting client input.
    provider_config = PROVIDER_CREDENTIALS.get(provider_reference)

    # Rejects a valid key that is not authorized for a configured provider credential.
    if provider_config is None:
        # Uses 403 because authentication succeeded but authorization did not.
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "This virtual key cannot use the requested provider.",
                    "type": "authorization_error",
                    "code": "provider_not_allowed",
                }
            },
        )

    # Reads the resolved provider URL from trusted server configuration.
    provider_url = provider_config["url"]

    # Reads the resolved real API key from trusted server configuration.
    provider_api_key = provider_config["api_key"]

    # Detects incomplete server configuration before attempting an outbound call.
    if provider_url is None or provider_api_key is None:
        # Returns a sanitized error without exposing environment details.
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "The authorized provider is not configured.",
                    "type": "server_configuration_error",
                    "code": "provider_not_configured",
                }
            },
        )

    # Parses the client's JSON model, messages, stream flag, and generation options.
    body = await request.json()

    # Enables streaming only when the caller explicitly sends the JSON boolean true.
    stream_requested = isinstance(body, dict) and body.get("stream") is True

    # Keeps Phase 2 behavior for clients and background tasks that expect one JSON body.
    if not stream_requested:
        # Opens a short-lived asynchronous client for the normal buffered request.
        async with create_provider_client() as provider_client:
            # Converts provider network failures into a stable gateway response.
            try:
                # Sends the unchanged JSON and waits for the complete provider response.
                provider_response = await provider_client.post(
                    provider_url,
                    headers=build_provider_headers(provider_api_key),
                    json=body,
                )

            # Handles DNS, connection, TLS, and timeout failures from HTTPX.
            except httpx.RequestError:
                # Returns one reusable sanitized error without leaking configuration.
                return provider_connection_error()

        # Returns the provider's complete body and status for non-streaming requests.
        return Response(
            content=provider_response.content,
            status_code=provider_response.status_code,
            headers=get_provider_response_headers(provider_response),
        )

    # Keeps this client open after the route returns because streaming continues later.
    provider_client = create_provider_client()

    # Converts failures while opening the provider stream into a stable gateway response.
    try:
        # Builds the request separately so HTTPX can send it in streaming mode.
        provider_request = provider_client.build_request(
            "POST",
            provider_url,
            headers=build_provider_headers(provider_api_key),
            json=body,
        )

        # Reads only provider headers now; response bytes remain unbuffered.
        provider_response = await provider_client.send(
            provider_request,
            stream=True,
        )

    # Handles DNS, connection, TLS, and timeout failures before streaming begins.
    except httpx.RequestError:
        # Closes the manually managed client because no generator owns it yet.
        await provider_client.aclose()

        # Returns the same sanitized gateway error used by normal requests.
        return provider_connection_error()

    # Streams every provider SSE chunk with its original status and useful headers.
    return StreamingResponse(
        stream_provider_body(request, provider_response, provider_client),
        status_code=provider_response.status_code,
        headers=get_provider_response_headers(provider_response, raw_body=True),
    )
