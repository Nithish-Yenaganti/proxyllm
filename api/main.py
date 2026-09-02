"""Run the authenticated, streaming, multi-provider ProxyLLM HTTP API."""

# Parses incoming JSON while converting malformed bodies into stable client errors.
import json

# Reads real provider credentials and optional endpoint overrides from the environment.
import os

# Provides an asynchronous startup and shutdown context for FastAPI.
from contextlib import asynccontextmanager

# Loads local development variables from the ignored .env file.
from dotenv import load_dotenv

# Provides the web application and incoming request type.
from fastapi import FastAPI, Request

# Provides complete JSON responses and unbuffered streaming responses.
from fastapi.responses import JSONResponse, Response, StreamingResponse

# Protects every /v1/* route with active virtual API keys.
from api.middleware import VirtualKeyAuthMiddleware

# Supplies database initialization and per-provider authorization lookups.
from auth.database import get_provider_permission_for_key, initialize_database

# Supplies the provider-independent adapter request and error types.
from providers.base import (
    AdapterRequest,
    ProviderConnectionError,
    ProviderCredential,
    ProviderRequestError,
)

# Resolves public model names and concrete provider implementations.
from providers.registry import get_model_route, get_provider_adapter


# Loads .env values into process memory without displaying or logging their contents.
load_dotenv()


# Maps authorized database references to real server-side provider configuration.
PROVIDER_CREDENTIALS = {
    # Resolves Fireworks permissions created during Phase 2 and later phases.
    ("fireworks", "default"): {
        # Reads the real Fireworks secret used only for outbound provider requests.
        "api_key": os.getenv("FIREWORK_API_KEY"),
        # Reads the existing Fireworks chat-completions endpoint.
        "url": os.getenv("FIREWORK_URL"),
    },
    # Resolves Anthropic permissions granted during Phase 4.
    ("anthropic", "default"): {
        # Reads the real Anthropic secret used only by the Anthropic adapter.
        "api_key": os.getenv("ANTHROPIC_API_KEY"),
        # Allows a custom endpoint while defaulting to Anthropic's direct Messages API.
        "url": os.getenv(
            "ANTHROPIC_URL",
            "https://api.anthropic.com/v1/messages",
        ),
    },
}


# Creates one OpenAI-compatible gateway error response.
def gateway_error(
    status_code: int,
    message: str,
    error_type: str,
    code: str,
) -> JSONResponse:
    # Keeps errors predictable for every OpenAI-compatible client application.
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        },
    )


# Performs one-time asynchronous application startup work.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Creates and migrates the key and provider-permission tables when necessary.
    await initialize_database()

    # Hands control to FastAPI for the lifetime of the running server.
    yield


# Creates the Uvicorn-served application with database startup enabled.
app = FastAPI(lifespan=lifespan)

# Applies virtual-key authentication before protected routes execute.
app.add_middleware(VirtualKeyAuthMiddleware)


# Registers a public route that confirms the process is reachable.
@app.get("/")
def read_root():
    # Returns a small health response without requiring authentication.
    return {"message": "Hello, World!"}


# Accepts the unified OpenAI-compatible endpoint used by every client application.
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Reads safe key identity metadata attached by successful authentication middleware.
    virtual_key_record = request.state.virtual_key

    # Parses the OpenAI-compatible request body before model routing begins.
    try:
        # Converts incoming UTF-8 JSON into ordinary Python values.
        body = await request.json()

    # Handles malformed JSON without exposing an internal FastAPI exception.
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Returns the public API's standard invalid-request response.
        return gateway_error(
            400,
            "The request body must contain valid JSON.",
            "invalid_request_error",
            "invalid_json",
        )

    # Requires a JSON object because adapters read named request fields.
    if not isinstance(body, dict):
        # Rejects arrays, strings, numbers, booleans, and null before routing.
        return gateway_error(
            400,
            "The request body must be a JSON object.",
            "invalid_request_error",
            "invalid_request_body",
        )

    # Reads the public model name that controls Phase 4 provider routing.
    requested_model = body.get("model")

    # Requires one non-empty model name before consulting the explicit registry.
    if not isinstance(requested_model, str) or requested_model == "":
        # Gives clients a stable validation error instead of a provider-specific failure.
        return gateway_error(
            400,
            "The model field must be a non-empty string.",
            "invalid_request_error",
            "invalid_model",
        )

    # Resolves the provider and real model using an explicit allowlisted mapping.
    model_route = get_model_route(requested_model)

    # Rejects unknown models rather than guessing a provider from their name.
    if model_route is None:
        # Uses 404 because the requested gateway model does not exist.
        return gateway_error(
            404,
            f"The model {requested_model!r} is not configured.",
            "invalid_request_error",
            "model_not_found",
        )

    # Looks up this active virtual key's permission for the routed provider.
    provider_permission = await get_provider_permission_for_key(
        int(virtual_key_record["id"]),
        model_route.provider,
    )

    # Rejects a valid key that lacks authorization for the selected provider.
    if provider_permission is None:
        # Uses 403 because authentication succeeded but provider authorization failed.
        return gateway_error(
            403,
            "This virtual key cannot use the provider required by that model.",
            "authorization_error",
            "provider_not_allowed",
        )

    # Builds the trusted provider credential reference stored in SQLite.
    provider_reference = (
        model_route.provider,
        provider_permission["provider_credential"],
    )

    # Resolves the real endpoint and API key without accepting either from the client.
    provider_config = PROVIDER_CREDENTIALS.get(provider_reference)

    # Rejects database permissions that have no matching server configuration.
    if provider_config is None:
        # Reports an internal deployment mismatch without revealing credential details.
        return gateway_error(
            500,
            "The authorized provider credential is not configured.",
            "server_configuration_error",
            "provider_not_configured",
        )

    # Reads the real endpoint and secret from the trusted server configuration.
    provider_url = provider_config["url"]
    provider_api_key = provider_config["api_key"]

    # Detects missing or empty provider configuration before opening a connection.
    if (
        not isinstance(provider_url, str)
        or provider_url == ""
        or not isinstance(provider_api_key, str)
        or provider_api_key == ""
    ):
        # Returns a sanitized error without exposing environment variable contents.
        return gateway_error(
            500,
            "The authorized provider is not configured.",
            "server_configuration_error",
            "provider_not_configured",
        )

    # Resolves the concrete adapter selected by the trusted model route.
    provider_adapter = get_provider_adapter(model_route.provider)

    # Handles an invalid server registry without treating it as a client mistake.
    if provider_adapter is None:
        # Reports a deployment error without revealing internal class names.
        return gateway_error(
            500,
            "The routed provider adapter is not configured.",
            "server_configuration_error",
            "adapter_not_configured",
        )

    # Packages the unified request and authorized credential for the selected adapter.
    adapter_request = AdapterRequest(
        body=body,
        public_model=requested_model,
        upstream_model=model_route.upstream_model,
        credential=ProviderCredential(
            url=provider_url,
            api_key=provider_api_key,
        ),
        is_disconnected=request.is_disconnected,
    )

    # Runs provider-specific translation and HTTP work behind the shared interface.
    try:
        # Receives one provider-independent response container from either adapter.
        adapter_response = await provider_adapter.send(adapter_request)

    # Converts unsupported but well-formed translations into a client error.
    except ProviderRequestError as error:
        # Returns the adapter's safe explanation without exposing provider credentials.
        return gateway_error(
            400,
            str(error),
            "invalid_request_error",
            "unsupported_request",
        )

    # Converts DNS, TLS, connection, timeout, and invalid upstream responses to 502.
    except ProviderConnectionError:
        # Uses one stable message regardless of which provider failed.
        return gateway_error(
            502,
            "The upstream provider could not complete the request.",
            "provider_connection_error",
            "provider_unavailable",
        )

    # Uses FastAPI's lazy response type when the adapter returned an async byte iterator.
    if adapter_response.streaming:
        # Starts the downstream response before consuming the provider's full body.
        return StreamingResponse(
            adapter_response.body,
            status_code=adapter_response.status_code,
            headers=adapter_response.headers,
        )

    # Returns one complete normalized or OpenAI-compatible response body.
    return Response(
        content=adapter_response.body,
        status_code=adapter_response.status_code,
        headers=adapter_response.headers,
    )
