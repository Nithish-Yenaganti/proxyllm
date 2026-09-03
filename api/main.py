"""Run the authenticated, streaming, multi-provider ProxyLLM HTTP API."""

# Parses incoming JSON while converting malformed bodies into stable client errors.
import json

# Reports internal logging failures without exposing them in client responses.
import logging

# Reads real provider credentials and optional endpoint overrides from the environment.
import os

# Provides an asynchronous startup and shutdown context for FastAPI.
from contextlib import asynccontextmanager

# Preserves exact cost values while moving them between pricing and cache metadata.
from decimal import Decimal

# Measures provider and streaming duration with a monotonic high-resolution clock.
from time import perf_counter, time

# Loads local development variables from the ignored .env file.
from dotenv import load_dotenv

# Provides the web application and incoming request type.
from fastapi import FastAPI, Request

# Provides complete JSON responses and unbuffered streaming responses.
from fastapi.responses import JSONResponse, Response, StreamingResponse

# Protects every /v1/* route with active virtual API keys.
from api.middleware import VirtualKeyAuthMiddleware

# Supplies database initialization, authorization lookups, and usage persistence.
from auth.database import (
    create_usage_log_record,
    get_cached_response_record,
    get_provider_permission_for_key,
    initialize_database,
    upsert_cached_response_record,
)

# Supplies the provider-independent adapter request and error types.
from providers.base import (
    AdapterRequest,
    ProviderConnectionError,
    ProviderCredential,
    ProviderRequestError,
)

# Resolves public model names, prices, and concrete provider implementations.
from providers.registry import ModelRoute, get_model_route, get_provider_adapter

# Applies safe cache eligibility, provider-field cleanup, and privacy-scoped hashing.
from usage.cache import build_cache_key, build_provider_body, is_cache_eligible

# Observes usage in complete and streamed OpenAI-compatible provider responses.
from usage.tracking import (
    OpenAIStreamObserver,
    StreamObservation,
    TokenUsage,
    estimate_cost_usd,
    extract_token_usage_from_body,
    observe_stream,
)


# Loads .env values into process memory without displaying or logging their contents.
load_dotenv()


# Creates this module's internal logger without printing request bodies or secrets.
logger = logging.getLogger(__name__)


# Keeps deterministic cached responses for one hour before requiring fresh generation.
CACHE_TTL_SECONDS = 3600


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


# Persists one safe Phase 5.1 accounting record without affecting the API response.
async def record_request_usage(
    virtual_key_id: int,
    provider: str | None,
    model: str | None,
    model_route: ModelRoute | None,
    usage: TokenUsage,
    started_at: float,
    status: str,
    status_code: int,
    cache_status: str = "not_eligible",
    cost_avoided_usd: Decimal = Decimal("0"),
) -> None:
    # Uses a zero estimate when validation ended before a priced model was selected.
    estimated_cost = (
        estimate_cost_usd(
            usage,
            model_route.input_cost_per_million,
            model_route.output_cost_per_million,
            model_route.cached_input_cost_per_million,
        )
        if model_route is not None
        else 0
    )

    # Converts monotonic elapsed seconds into a readable non-negative millisecond value.
    latency_ms = max((perf_counter() - started_at) * 1000, 0)

    # Keeps observability storage failures from replacing a valid provider response.
    try:
        # Writes identifiers and metrics only; prompts, replies, and secrets are excluded.
        await create_usage_log_record(
            virtual_key_id=virtual_key_id,
            provider=provider,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=float(estimated_cost),
            latency_ms=latency_ms,
            status=status,
            status_code=status_code,
            cache_status=cache_status,
            cost_avoided_usd=float(cost_avoided_usd),
        )

    # Logs safe diagnostics locally while allowing the original request to finish.
    except Exception:
        # Never includes request content, virtual keys, or real provider credentials.
        logger.exception("Unable to persist usage metrics")


# Loads one cached response while allowing provider traffic if SQLite is unavailable.
async def read_cached_response(cache_key: str) -> dict[str, object] | None:
    # Keeps cache infrastructure optional to core provider availability.
    try:
        # Rejects expired rows inside the same indexed SQLite query.
        return await get_cached_response_record(cache_key, time())

    # Logs safe cache diagnostics without exposing the hashed request or its content.
    except Exception:
        # A cache read failure becomes a provider miss instead of an API outage.
        logger.exception("Unable to read the response cache")

        # None instructs the route to continue to the authorized provider.
        return None


# Stores one successful response while allowing it to reach the client on failure.
async def write_cached_response(
    cache_key: str,
    virtual_key_id: int,
    provider: str,
    model: str,
    response_status_code: int,
    response_body: bytes,
    response_headers: dict[str, str],
    estimated_cost_usd: Decimal,
) -> bool:
    # Captures one timestamp so TTL duration remains exact.
    created_at_unix = time()

    # Keeps cache persistence outside the correctness path of provider responses.
    try:
        # Saves safe response data and the original estimated provider cost.
        await upsert_cached_response_record(
            cache_key=cache_key,
            virtual_key_id=virtual_key_id,
            provider=provider,
            model=model,
            response_status_code=response_status_code,
            response_body=response_body,
            response_headers_json=json.dumps(response_headers, sort_keys=True),
            estimated_cost_usd=float(estimated_cost_usd),
            created_at_unix=created_at_unix,
            expires_at_unix=created_at_unix + CACHE_TTL_SECONDS,
        )

    # Logs safe diagnostics while preserving the successful provider response.
    except Exception:
        # Does not print the cache key because it is derived from request content.
        logger.exception("Unable to write the response cache")

        # Reports failure so the response header can say caching was unavailable.
        return False

    # Confirms future identical requests may reuse this response.
    return True


# Validates response metadata loaded from SQLite before sending it to a client.
def decode_cached_headers(serialized_headers: object) -> dict[str, str]:
    # Requires the cache schema's JSON text representation.
    if not isinstance(serialized_headers, str):
        # Falls back to the known response-body representation.
        return {"Content-Type": "application/json"}

    # Parses the stored header object while tolerating corrupted cache rows.
    try:
        # Converts the JSON text back into ordinary Python values.
        decoded_headers = json.loads(serialized_headers)

    # Uses a safe content type instead of failing a valid authenticated request.
    except json.JSONDecodeError:
        # Avoids propagating malformed internal metadata.
        return {"Content-Type": "application/json"}

    # Accepts only a flat string-to-string header mapping.
    if not isinstance(decoded_headers, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in decoded_headers.items()
    ):
        # Rejects arrays, nested objects, and non-string header values.
        return {"Content-Type": "application/json"}

    # Returns a copy that the route can safely add cache diagnostic headers to.
    return dict(decoded_headers)


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
    # Starts latency measurement before parsing, routing, and provider work.
    started_at = perf_counter()

    # Reads safe key identity metadata attached by successful authentication middleware.
    virtual_key_record = request.state.virtual_key

    # Converts the authenticated key's SQLite identifier once for all log paths.
    virtual_key_id = int(virtual_key_record["id"])

    # Parses the OpenAI-compatible request body before model routing begins.
    try:
        # Converts incoming UTF-8 JSON into ordinary Python values.
        body = await request.json()

    # Handles malformed JSON without exposing an internal FastAPI exception.
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Records the rejected authenticated call even though no model could be read.
        await record_request_usage(
            virtual_key_id,
            None,
            None,
            None,
            TokenUsage(),
            started_at,
            "invalid_request",
            400,
        )

        # Returns the public API's standard invalid-request response.
        return gateway_error(
            400,
            "The request body must contain valid JSON.",
            "invalid_request_error",
            "invalid_json",
        )

    # Requires a JSON object because adapters read named request fields.
    if not isinstance(body, dict):
        # Records a body-shape failure without storing the submitted JSON value.
        await record_request_usage(
            virtual_key_id,
            None,
            None,
            None,
            TokenUsage(),
            started_at,
            "invalid_request",
            400,
        )

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
        # Records the validation failure without treating a malformed value as a model.
        await record_request_usage(
            virtual_key_id,
            None,
            None,
            None,
            TokenUsage(),
            started_at,
            "invalid_request",
            400,
        )

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
        # Records the requested public name while leaving its unknown provider empty.
        await record_request_usage(
            virtual_key_id,
            None,
            requested_model,
            None,
            TokenUsage(),
            started_at,
            "model_not_found",
            404,
        )

        # Uses 404 because the requested gateway model does not exist.
        return gateway_error(
            404,
            f"The model {requested_model!r} is not configured.",
            "invalid_request_error",
            "model_not_found",
        )

    # Looks up this active virtual key's permission for the routed provider.
    provider_permission = await get_provider_permission_for_key(
        virtual_key_id,
        model_route.provider,
    )

    # Rejects a valid key that lacks authorization for the selected provider.
    if provider_permission is None:
        # Records denied authorization with zero usage because no provider call occurred.
        await record_request_usage(
            virtual_key_id,
            model_route.provider,
            requested_model,
            model_route,
            TokenUsage(),
            started_at,
            "denied",
            403,
        )

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
        # Records the deployment mismatch before returning its sanitized server error.
        await record_request_usage(
            virtual_key_id,
            model_route.provider,
            requested_model,
            model_route,
            TokenUsage(),
            started_at,
            "configuration_error",
            500,
        )

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
        # Records missing trusted configuration without revealing which value was absent.
        await record_request_usage(
            virtual_key_id,
            model_route.provider,
            requested_model,
            model_route,
            TokenUsage(),
            started_at,
            "configuration_error",
            500,
        )

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
        # Records the invalid server registry before returning a stable deployment error.
        await record_request_usage(
            virtual_key_id,
            model_route.provider,
            requested_model,
            model_route,
            TokenUsage(),
            started_at,
            "configuration_error",
            500,
        )

        # Reports a deployment error without revealing internal class names.
        return gateway_error(
            500,
            "The routed provider adapter is not configured.",
            "server_configuration_error",
            "adapter_not_configured",
        )

    # Removes the gateway-only cache flag before either hashing or provider translation.
    provider_body = build_provider_body(body)

    # Allows caching only for complete responses with deterministic or explicit intent.
    cache_eligible = is_cache_eligible(body)

    # Starts with the value used by streams and non-deterministic requests.
    cache_status = "not_eligible"

    # Holds the privacy-scoped request hash only when this request may use the cache.
    cache_key: str | None = None

    # Checks persistent cached output before creating an outbound provider connection.
    if cache_eligible:
        # Includes the virtual-key owner to prevent cross-application response leakage.
        cache_key = build_cache_key(virtual_key_id, provider_body)

        # Reads one exact unexpired response or None for a normal miss.
        cached_response = await read_cached_response(cache_key)

        # Reuses the provider response only when all required stored fields are valid.
        if cached_response is not None:
            # Loads the cached response body from SQLite's BLOB representation.
            cached_body_value = cached_response.get("response_body")

            # Loads the original successful provider status code.
            cached_status_code = cached_response.get("response_status_code")

            # Requires bytes and a successful integer status before replaying the entry.
            if (
                isinstance(cached_body_value, bytes)
                and isinstance(cached_status_code, int)
                and 200 <= cached_status_code < 300
            ):
                # Restores only the safe response headers originally stored by the route.
                cached_headers = decode_cached_headers(
                    cached_response.get("response_headers_json")
                )

                # Identifies the response source for clients and benchmark tooling.
                cached_headers["X-Proxy-Cache"] = "HIT"

                # Reads the provider cost avoided by serving this stored response.
                cost_avoided = Decimal(
                    str(cached_response.get("estimated_cost_usd", 0))
                )

                # Exposes a safe numeric measurement without revealing request content.
                cached_headers["X-Proxy-Cost-Avoided-USD"] = format(
                    cost_avoided,
                    "f",
                )

                # Records zero new provider tokens and the estimated avoided cost.
                await record_request_usage(
                    virtual_key_id,
                    model_route.provider,
                    requested_model,
                    model_route,
                    TokenUsage(),
                    started_at,
                    "success",
                    cached_status_code,
                    cache_status="hit",
                    cost_avoided_usd=cost_avoided,
                )

                # Returns before adapter.send(), proving the provider was not called.
                return Response(
                    content=cached_body_value,
                    status_code=cached_status_code,
                    headers=cached_headers,
                )

        # Marks every usable but absent, expired, or invalid entry as a provider miss.
        cache_status = "miss"

    # Shares whether the adapter observed a downstream disconnect with the log wrapper.
    connection_state = {"disconnected": False}

    # Wraps FastAPI's check so disconnect outcomes can be classified after streaming.
    async def track_client_disconnect() -> bool:
        # Asks the incoming request whether its client connection has closed.
        is_disconnected = await request.is_disconnected()

        # Remembers any positive observation for the final immutable usage record.
        if is_disconnected:
            # A later false result cannot erase an already observed disconnect.
            connection_state["disconnected"] = True

        # Gives the adapter the same boolean used by its existing cleanup logic.
        return is_disconnected

    # Packages the unified request and authorized credential for the selected adapter.
    adapter_request = AdapterRequest(
        body=provider_body,
        public_model=requested_model,
        upstream_model=model_route.upstream_model,
        credential=ProviderCredential(
            url=provider_url,
            api_key=provider_api_key,
        ),
        is_disconnected=track_client_disconnect,
    )

    # Runs provider-specific translation and HTTP work behind the shared interface.
    try:
        # Receives one provider-independent response container from either adapter.
        adapter_response = await provider_adapter.send(adapter_request)

    # Converts unsupported but well-formed translations into a client error.
    except ProviderRequestError as error:
        # Records translation rejection before returning the adapter's safe explanation.
        await record_request_usage(
            virtual_key_id,
            model_route.provider,
            requested_model,
            model_route,
            TokenUsage(),
            started_at,
            "invalid_request",
            400,
            cache_status=cache_status,
        )

        # Returns the adapter's safe explanation without exposing provider credentials.
        return gateway_error(
            400,
            str(error),
            "invalid_request_error",
            "unsupported_request",
        )

    # Converts DNS, TLS, connection, timeout, and invalid upstream responses to 502.
    except ProviderConnectionError:
        # Records a provider connection failure with no fabricated token usage.
        await record_request_usage(
            virtual_key_id,
            model_route.provider,
            requested_model,
            model_route,
            TokenUsage(),
            started_at,
            "provider_error",
            502,
            cache_status=cache_status,
        )

        # Uses one stable message regardless of which provider failed.
        return gateway_error(
            502,
            "The upstream provider could not complete the request.",
            "provider_connection_error",
            "provider_unavailable",
        )

    # Uses FastAPI's lazy response type when the adapter returned an async byte iterator.
    if adapter_response.streaming:
        # Observes normalized SSE events without delaying or changing forwarded chunks.
        stream_observer = OpenAIStreamObserver(
            adapter_response.headers.get("Content-Encoding")
        )

        # Converts the final stream facts into exactly one durable usage row.
        async def finish_stream_log(
            observation: StreamObservation,
            stream_failed: bool,
        ) -> None:
            # Gives a downstream disconnect priority over generic truncation.
            if connection_state["disconnected"]:
                # Identifies a caller that stopped consuming tokens early.
                stream_status = "client_disconnected"

            # Identifies an exception raised while reading the provider stream.
            elif stream_failed:
                # Separates runtime stream failures from provider error events.
                stream_status = "stream_error"

            # Identifies an error envelope carried inside a successful SSE connection.
            elif stream_observer.observation.provider_error:
                # Marks the provider-generated terminal failure for reporting.
                stream_status = "provider_error"

            # Recognizes a clean OpenAI-compatible terminal marker.
            elif stream_observer.observation.completed:
                # Marks the fully consumed stream as successful.
                stream_status = "success"

            # Treats silent iterator exhaustion without [DONE] as truncated output.
            else:
                # Makes incomplete streams visible instead of counting them as success.
                stream_status = "incomplete"

            # Persists counters collected from the final usage-bearing SSE event.
            await record_request_usage(
                virtual_key_id,
                model_route.provider,
                requested_model,
                model_route,
                observation.usage,
                started_at,
                stream_status,
                adapter_response.status_code,
                cache_status=cache_status,
            )

        # Starts the downstream response before consuming the provider's full body.
        return StreamingResponse(
            observe_stream(
                adapter_response.body,
                stream_observer,
                finish_stream_log,
            ),
            status_code=adapter_response.status_code,
            headers=adapter_response.headers,
        )

    # Extracts normalized token totals from the complete provider response body.
    response_usage = extract_token_usage_from_body(adapter_response.body)

    # Treats any upstream 2xx result as a completed request for accounting purposes.
    response_status = (
        "success"
        if 200 <= adapter_response.status_code < 300
        else "provider_error"
    )

    # Calculates the cost once for cache storage and safe diagnostic headers.
    response_cost = estimate_cost_usd(
        response_usage,
        model_route.input_cost_per_million,
        model_route.output_cost_per_million,
        model_route.cached_input_cost_per_million,
    )

    # Copies adapter headers before adding gateway-owned cache diagnostics.
    response_headers = dict(adapter_response.headers)

    # Makes cache behavior directly measurable by clients and workload scripts.
    response_headers["X-Proxy-Cache"] = (
        "MISS" if cache_status == "miss" else "BYPASS"
    )

    # Exposes the estimated provider spend for this newly completed request.
    response_headers["X-Proxy-Estimated-Cost-USD"] = format(
        response_cost,
        "f",
    )

    # Stores only successful eligible responses after their complete body is available.
    if (
        cache_key is not None
        and 200 <= adapter_response.status_code < 300
        and isinstance(adapter_response.body, bytes)
    ):
        # Persists the response for this app and exact normalized request until TTL expiry.
        cache_written = await write_cached_response(
            cache_key,
            virtual_key_id,
            model_route.provider,
            requested_model,
            adapter_response.status_code,
            adapter_response.body,
            adapter_response.headers,
            response_cost,
        )

        # Distinguishes a normal miss from cache infrastructure that could not store it.
        if not cache_written:
            # Reports the degraded cache path while preserving the provider response.
            response_headers["X-Proxy-Cache"] = "ERROR"

    # Writes the complete-response log before returning the already-buffered body.
    await record_request_usage(
        virtual_key_id,
        model_route.provider,
        requested_model,
        model_route,
        response_usage,
        started_at,
        response_status,
        adapter_response.status_code,
        cache_status=cache_status,
    )

    # Returns one complete normalized or OpenAI-compatible response body.
    return Response(
        content=adapter_response.body,
        status_code=adapter_response.status_code,
        headers=response_headers,
    )
