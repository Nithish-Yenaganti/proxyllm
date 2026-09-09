"""Translate between the gateway's OpenAI format and Anthropic's Messages API."""

# Supplies asynchronous iterator and injectable HTTP client factory types.
from collections.abc import AsyncIterator, Callable

# Encodes normalized JSON responses and parses Anthropic SSE event data.
import json

# Creates OpenAI-compatible integer timestamps for normalized response objects.
import time

# Supplies flexible JSON dictionary annotations used during schema translation.
from typing import Any

# Sends buffered and streamed requests to Anthropic's Messages API.
import httpx
import anyio

# Supplies the shared adapter contract, errors, containers, and HTTP helpers.
from providers.base import (
    AdapterRequest,
    AdapterResponse,
    ProviderConnectionError,
    ProviderRequestError,
    create_http_client,
    get_response_headers,
)


# Supplies the required stable Anthropic API version header for direct HTTP requests.
ANTHROPIC_API_VERSION = "2023-06-01"

# Supplies a conservative output limit when an OpenAI-style request omits one.
DEFAULT_MAX_TOKENS = 1024


# Converts supported OpenAI message content into plain text for Anthropic.
def extract_text_content(content: object) -> str:
    # Accepts the common simple-message representation directly.
    if isinstance(content, str):
        # Returns the caller's text without changing it.
        return content

    # Supports OpenAI multipart content only when every part is textual.
    if isinstance(content, list):
        # Collects text parts in their original order.
        text_parts: list[str] = []

        # Examines each multipart content item before translation.
        for part in content:
            # Rejects malformed parts and non-text capabilities not implemented yet.
            if (
                not isinstance(part, dict)
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
            ):
                # Gives the client a clear boundary instead of silently losing content.
                raise ProviderRequestError(
                    "Anthropic routing currently supports text message content only."
                )

            # Preserves the text from this valid content part.
            text_parts.append(part["text"])

        # Joins adjacent text parts into Anthropic's accepted string shorthand.
        return "".join(text_parts)

    # Rejects null, numeric, object, and other unsupported content shapes.
    raise ProviderRequestError(
        "Every message must contain text or a list of text parts."
    )


# Converts one unified OpenAI-compatible request into Anthropic Messages JSON.
def validate_anthropic_settings(body: dict[str, Any]) -> None:
    supported = {"model", "messages", "max_tokens", "max_completion_tokens", "stream", "stop", "stream_options"}
    unsupported = sorted(set(body) - supported)
    if unsupported:
        raise ProviderRequestError(
            "This gateway's Anthropic adapter does not support these settings: "
            + ", ".join(unsupported)
            + ". Remove them or choose a compatible route."
        )
    if "max_tokens" in body and "max_completion_tokens" in body:
        raise ProviderRequestError("Supply only one token-limit setting.")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise ProviderRequestError("stream must be a boolean.")
    if "stream_options" in body:
        options = body["stream_options"]
        if (not isinstance(options, dict) or set(options) - {"include_usage"}
                or options.get("include_usage", True) is not True):
            raise ProviderRequestError("Anthropic stream_options supports only include_usage=true.")


def translate_anthropic_request(request: AdapterRequest) -> dict[str, Any]:
    validate_anthropic_settings(request.body)
    # Reads the required conversation list from the public request body.
    messages = request.body.get("messages")

    # Prevents malformed input from reaching the provider with a confusing error.
    if not isinstance(messages, list) or not messages:
        # Reports the same requirement expected by the public chat endpoint.
        raise ProviderRequestError("messages must be a non-empty array.")

    # Rejects tool schemas until request and response tool normalization is implemented.
    if "tools" in request.body or "tool_choice" in request.body:
        # Avoids forwarding a schema that Anthropic would interpret differently.
        raise ProviderRequestError(
            "Anthropic tool calling is not supported by this gateway yet."
        )

    # Holds top-level Anthropic system instructions extracted from OpenAI messages.
    system_parts: list[str] = []

    # Holds only user and assistant turns accepted by Anthropic's messages field.
    anthropic_messages: list[dict[str, str]] = []

    # Translates each conversation entry in its original order.
    for message in messages:
        # Requires the object shape used by OpenAI-compatible message arrays.
        if not isinstance(message, dict):
            # Stops before any provider request when a message is malformed.
            raise ProviderRequestError("Every message must be a JSON object.")

        # Reads the role used to determine Anthropic's target field.
        role = message.get("role")

        # Converts supported text content while rejecting lossy translations.
        content = extract_text_content(message.get("content"))

        # Moves OpenAI system and developer instructions to Anthropic's system field.
        if role in {"system", "developer"}:
            # Preserves multiple instruction messages in their original order.
            system_parts.append(content)

            # Prevents system instructions from appearing as conversation turns.
            continue

        # Accepts the two conversation roles supported by Anthropic Messages.
        if role not in {"user", "assistant"}:
            # Rejects tool and custom roles that require a richer translation layer.
            raise ProviderRequestError(
                f"Anthropic routing does not support the message role {role!r}."
            )

        # Adds the translated conversation turn to the provider request.
        anthropic_messages.append({"role": role, "content": content})

    # Requires at least one conversational turn after extracting instructions.
    if not anthropic_messages:
        # Prevents a provider call containing only system instructions.
        raise ProviderRequestError(
            "Anthropic routing requires at least one user or assistant message."
        )

    # Accepts either common OpenAI name for the maximum generated-token limit.
    max_tokens = request.body.get(
        "max_tokens",
        request.body.get("max_completion_tokens", DEFAULT_MAX_TOKENS),
    )

    # Validates the common numeric limit before constructing provider JSON.
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        # Gives the caller a stable client error instead of a provider-specific one.
        raise ProviderRequestError("max_tokens must be a positive integer.")

    # Creates the minimum Anthropic Messages request shared by both response modes.
    provider_body: dict[str, Any] = {
        "model": request.upstream_model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "stream": request.body.get("stream") is True,
    }

    # Adds the top-level Anthropic system field only when instructions were supplied.
    if system_parts:
        # Separates independent instructions without changing their internal text.
        provider_body["system"] = "\n\n".join(system_parts)

    # Reads OpenAI's singular string or array stop field when supplied.
    stop = request.body.get("stop")

    # Converts one OpenAI stop string into Anthropic's string array.
    if isinstance(stop, str):
        # Adds exactly one custom stop sequence.
        provider_body["stop_sequences"] = [stop]

    # Validates and copies a list of custom stop sequences.
    elif isinstance(stop, list):
        # Requires every sequence to be a string before forwarding it.
        if not all(isinstance(item, str) for item in stop):
            # Prevents mixed arrays from reaching the provider.
            raise ProviderRequestError("stop must contain only strings.")

        # Copies the list so later mutations cannot change the provider request.
        provider_body["stop_sequences"] = list(stop)

    # Rejects non-null stop values that cannot be translated.
    elif stop is not None:
        # Reports the two supported public representations.
        raise ProviderRequestError("stop must be a string or an array of strings.")

    # Unsupported settings were explicitly rejected before translation.
    return provider_body


# Maps Anthropic stop reasons to OpenAI-compatible finish reasons.
def normalize_finish_reason(stop_reason: object) -> str:
    # Treats natural completion and custom sequences as normal stops.
    if stop_reason in {"end_turn", "stop_sequence"}:
        # Uses the OpenAI finish value expected by compatible clients.
        return "stop"

    # Identifies provider output that reached the configured token ceiling.
    if stop_reason == "max_tokens":
        # Uses OpenAI's standard length-limited finish value.
        return "length"

    # Preserves the conventional finish value if future tool translation is added.
    if stop_reason == "tool_use":
        # Matches the OpenAI chat-completions tool finish reason.
        return "tool_calls"

    # Treats provider-specific terminal reasons as completed responses for text clients.
    return "stop"


# Converts Anthropic usage fields into the OpenAI-compatible token summary.
def normalize_usage(usage: object) -> dict[str, int]:
    # Uses an empty mapping when Anthropic omits usage on an error or partial event.
    usage_data = usage if isinstance(usage, dict) else {}

    # Reads input tokens while rejecting non-integer values defensively.
    input_tokens = usage_data.get("input_tokens", 0)

    # Reads output tokens while rejecting non-integer values defensively.
    output_tokens = usage_data.get("output_tokens", 0)

    # Normalizes unexpected provider values to safe numeric zeros.
    prompt_tokens = input_tokens if isinstance(input_tokens, int) else 0
    completion_tokens = output_tokens if isinstance(output_tokens, int) else 0

    # Returns the token names expected by OpenAI-compatible client applications.
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# Converts one successful Anthropic Message object into a chat completion object.
def normalize_anthropic_response(
    provider_body: object,
    public_model: str,
) -> dict[str, Any]:
    # Requires the JSON object shape documented for Anthropic Message responses.
    if not isinstance(provider_body, dict):
        # Treats an unexpected successful schema as an upstream protocol failure.
        raise ProviderConnectionError("Anthropic returned an invalid JSON response.")

    # Reads Anthropic's array of typed response content blocks.
    content_blocks = provider_body.get("content", [])

    # Collects only text blocks for the gateway's current text-chat contract.
    text_parts: list[str] = []

    # Handles a missing or malformed content array without exposing an exception.
    if isinstance(content_blocks, list):
        # Visits every provider content block in its original order.
        for block in content_blocks:
            # Selects documented text blocks and ignores unrelated metadata blocks.
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                # Preserves the generated text exactly as Anthropic returned it.
                text_parts.append(block["text"])

    # Builds the OpenAI-compatible response shape understood by existing clients.
    return {
        "id": str(provider_body.get("id", "anthropic-message")),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                },
                "finish_reason": normalize_finish_reason(
                    provider_body.get("stop_reason")
                ),
            }
        ],
        "usage": normalize_usage(provider_body.get("usage")),
    }


# Converts an Anthropic error body into the gateway's OpenAI-compatible envelope.
def normalize_anthropic_error(provider_body: object) -> dict[str, Any]:
    # Reads the nested Anthropic error object when the provider supplied one.
    error_data = (
        provider_body.get("error")
        if isinstance(provider_body, dict)
        else None
    )

    # Uses a stable fallback when the upstream body is missing or malformed.
    if not isinstance(error_data, dict):
        # Creates a generic provider error without exposing raw upstream data.
        return {
            "error": {
                "message": "Anthropic returned an invalid error response.",
                "type": "provider_error",
                "code": "anthropic_error",
            }
        }

    # Reads the provider's safe human-readable error description.
    message = error_data.get("message", "Anthropic request failed.")

    # Reads the provider error category for compatible client diagnostics.
    error_type = error_data.get("type", "provider_error")

    # Returns the same outer error shape used by OpenAI-compatible APIs.
    return {
        "error": {
            "message": str(message),
            "type": str(error_type),
            "code": str(error_type),
        }
    }


# Encodes one OpenAI-compatible streaming object as an SSE data event.
def encode_openai_sse(data: dict[str, Any]) -> bytes:
    # Uses compact UTF-8 JSON followed by the blank line required by SSE framing.
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


# Creates one OpenAI-compatible chat-completion streaming chunk.
def create_openai_chunk(
    message_id: str,
    public_model: str,
    created: int,
    delta: dict[str, str],
    finish_reason: str | None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    # Builds the stable event fields expected by OpenAI-compatible chat clients.
    chunk: dict[str, Any] = {
        "id": message_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }

    # Adds token usage only to the final event when Anthropic supplied it.
    if usage is not None:
        # Keeps ordinary text delta events small and compatible.
        chunk["usage"] = usage

    # Returns the JSON object before SSE encoding.
    return chunk


# Translates Anthropic's named SSE events into OpenAI-compatible data-only events.
async def translate_anthropic_stream(
    request: AdapterRequest,
    provider_response: httpx.Response,
    provider_client: httpx.AsyncClient,
) -> AsyncIterator[bytes]:
    # Uses stable fallback values until the message_start event supplies real metadata.
    message_id = "anthropic-message"
    created = int(time.time())
    input_tokens = 0
    output_tokens = 0
    stop_reason: object = None

    # Guarantees upstream cleanup after completion, error, cancellation, or disconnect.
    try:
        # Reads complete SSE lines while allowing HTTPX to decode content compression.
        async for line in provider_response.aiter_lines():
            # Stops processing as soon as the downstream application disconnects.
            if await request.is_disconnected():
                # Leaves iteration so the finally block releases both resources.
                break

            # Ignores Anthropic's named event lines and blank SSE separators.
            if not line.startswith("data:"):
                # Continues until a JSON-bearing data line arrives.
                continue

            # Removes the SSE field name and optional leading whitespace.
            serialized_event = line.removeprefix("data:").strip()

            # Ignores empty data fields that carry no event object.
            if not serialized_event:
                # Waits for the next provider line.
                continue

            # Parses Anthropic's JSON event payload.
            try:
                # Converts the serialized provider event into a Python object.
                event = json.loads(serialized_event)

            # Handles malformed provider SSE without leaking raw event data.
            except json.JSONDecodeError:
                # Emits one normalized error before ending this broken stream.
                yield encode_openai_sse(
                    {
                        "error": {
                            "message": "Anthropic returned an invalid stream event.",
                            "type": "provider_error",
                            "code": "invalid_stream_event",
                        }
                    }
                )

                # Marks the stream complete for OpenAI-compatible clients.
                yield b"data: [DONE]\n\n"

                # Stops reading the invalid provider stream.
                return

            # Ignores JSON values that are not provider event objects.
            if not isinstance(event, dict):
                # Waits for the next valid provider event.
                continue

            # Selects the documented Anthropic streaming event variant.
            event_type = event.get("type")

            # Initializes OpenAI stream identity and role from message_start.
            if event_type == "message_start":
                # Reads the nested Anthropic Message object.
                message = event.get("message", {})

                # Uses the real message ID when present.
                if isinstance(message, dict):
                    # Preserves the provider ID for tracing across both protocols.
                    message_id = str(message.get("id", message_id))

                    # Reads the initial input-token count from Anthropic usage.
                    initial_usage = normalize_usage(message.get("usage"))
                    input_tokens = initial_usage["prompt_tokens"]

                # Sends the initial assistant role expected by OpenAI stream clients.
                yield encode_openai_sse(
                    create_openai_chunk(
                        message_id,
                        request.public_model,
                        created,
                        {"role": "assistant", "content": ""},
                        None,
                    )
                )

                # Waits for content delta events.
                continue

            # Converts Anthropic text deltas into OpenAI content deltas.
            if event_type == "content_block_delta":
                # Reads the typed delta object carried by this content event.
                delta = event.get("delta", {})

                # Emits only generated text; other block types are outside current scope.
                if (
                    isinstance(delta, dict)
                    and delta.get("type") == "text_delta"
                    and isinstance(delta.get("text"), str)
                ):
                    # Sends this token fragment immediately to the downstream client.
                    yield encode_openai_sse(
                        create_openai_chunk(
                            message_id,
                            request.public_model,
                            created,
                            {"content": delta["text"]},
                            None,
                        )
                    )

                # Ignores non-text content block deltas safely.
                continue

            # Records Anthropic's final reason and output usage before message_stop.
            if event_type == "message_delta":
                # Reads the nested stop metadata.
                delta = event.get("delta", {})

                # Saves a documented stop reason when one is present.
                if isinstance(delta, dict):
                    # Defers emission until the final OpenAI chunk.
                    stop_reason = delta.get("stop_reason", stop_reason)

                # Reads the latest cumulative Anthropic output-token count.
                final_usage = normalize_usage(event.get("usage"))
                output_tokens = final_usage["completion_tokens"]

                # Waits for message_stop before closing the downstream stream.
                continue

            # Completes the normalized OpenAI stream when Anthropic stops the message.
            if event_type == "message_stop":
                # Builds the final token totals from the two Anthropic usage events.
                usage = {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }

                # Emits the terminal chunk containing finish reason and token usage.
                yield encode_openai_sse(
                    create_openai_chunk(
                        message_id,
                        request.public_model,
                        created,
                        {},
                        normalize_finish_reason(stop_reason),
                        usage,
                    )
                )

                # Adds the completion marker required by OpenAI-compatible clients.
                yield b"data: [DONE]\n\n"

                # Stops after the documented terminal provider event.
                return

            # Converts provider-side stream errors into the gateway error envelope.
            if event_type == "error":
                # Emits the normalized error as an OpenAI-style SSE data event.
                yield encode_openai_sse(normalize_anthropic_error(event))

                # Finishes the downstream stream after the provider error.
                yield b"data: [DONE]\n\n"

                # Stops reading after a terminal error.
                return

    # Runs even if Starlette cancels the generator after a client disconnect.
    finally:
        # Releases the Anthropic response stream and its network connection.
        with anyio.CancelScope(shield=True):
            await provider_response.aclose()
            await provider_client.aclose()


# Adapts the gateway's OpenAI-compatible contract to Anthropic Messages.
class AnthropicAdapter:
    # Exposes the stable registry name for this provider implementation.
    name = "anthropic"

    # Stores a replaceable HTTP client factory for production and isolated tests.
    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] = create_http_client,
    ) -> None:
        # Keeps client construction outside translation and routing logic.
        self.client_factory = client_factory

    # Translates, sends, and normalizes one authorized Anthropic request.
    async def send(self, request: AdapterRequest) -> AdapterResponse:
        # Converts the unified request into the Anthropic Messages schema.
        provider_body = translate_anthropic_request(request)

        # Uses the direct HTTP authentication and version headers Anthropic requires.
        provider_headers = {
            "x-api-key": request.credential.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
        }

        # Selects streaming from the already-normalized provider body.
        stream_requested = provider_body["stream"] is True

        # Handles complete Anthropic responses without a streaming generator.
        if not stream_requested:
            # Releases a borrowed pool handle, or closes an independently owned test client.
            async with self.client_factory() as provider_client:
                # Converts provider network failures into a gateway-level exception.
                try:
                    # Sends the translated Messages request and waits for all JSON.
                    provider_response = await provider_client.post(
                        request.credential.url,
                        headers=provider_headers,
                        json=provider_body,
                    )

                # Handles DNS, TLS, connection, and timeout failures from HTTPX.
                except httpx.RequestError as error:
                    # Preserves the original exception as internal diagnostic context.
                    raise ProviderConnectionError from error

            # Parses the provider JSON after the complete response has arrived.
            try:
                # Reads the documented Message or error object.
                response_json = provider_response.json()

            # Handles a provider response that claims JSON but contains invalid data.
            except json.JSONDecodeError as error:
                # Converts the unexpected upstream schema into a gateway 502.
                raise ProviderConnectionError from error

            # Normalizes Anthropic error envelopes while preserving their HTTP status.
            if provider_response.is_error:
                # Converts the safe error object to compact response bytes.
                normalized_error = json.dumps(
                    normalize_anthropic_error(response_json),
                    separators=(",", ":"),
                ).encode("utf-8")

                # Returns a normal JSON response because provider errors are not SSE.
                return AdapterResponse(
                    status_code=provider_response.status_code,
                    headers={"Content-Type": "application/json"},
                    body=normalized_error,
                    streaming=False,
                )

            # Converts the successful Message object into OpenAI chat-completion JSON.
            normalized_response = json.dumps(
                normalize_anthropic_response(response_json, request.public_model),
                separators=(",", ":"),
            ).encode("utf-8")

            # Returns one complete normalized response to non-streaming clients.
            return AdapterResponse(
                status_code=provider_response.status_code,
                headers={"Content-Type": "application/json"},
                body=normalized_response,
                streaming=False,
            )

        # Keeps this HTTPX client open until translated streaming finishes.
        provider_client = self.client_factory()

        # Opens the provider stream while handling connection failures consistently.
        try:
            # Builds the request separately so HTTPX can leave the body unread.
            provider_http_request = provider_client.build_request(
                "POST",
                request.credential.url,
                headers=provider_headers,
                json=provider_body,
            )

            # Receives only upstream headers before returning the lazy stream body.
            provider_response = await provider_client.send(
                provider_http_request,
                stream=True,
            )

        # Handles failures that happen before Anthropic returns an HTTP response.
        except httpx.RequestError as error:
            # Releases the manually managed client because no generator owns it.
            await provider_client.aclose()

            # Lets the FastAPI layer produce the shared 502 error response.
            raise ProviderConnectionError from error

        except BaseException:
            with anyio.CancelScope(shield=True):
                await provider_client.aclose()
            raise

        # Reads provider errors normally because Anthropic may return JSON before SSE.
        if provider_response.is_error:
            # Buffers only the small error body rather than a successful generation.
            try:
                error_body = await provider_response.aread()
            except httpx.RequestError as error:
                raise ProviderConnectionError from error
            finally:
                with anyio.CancelScope(shield=True):
                    await provider_response.aclose()
                    await provider_client.aclose()

            # Attempts to parse the documented Anthropic error envelope.
            try:
                # Decodes UTF-8 JSON from the complete provider error body.
                error_json = json.loads(error_body)

            # Uses a stable fallback when the provider returned malformed JSON.
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Avoids exposing arbitrary upstream bytes to the client.
                error_json = None

            # Encodes the provider-independent error body expected by clients.
            normalized_error = json.dumps(
                normalize_anthropic_error(error_json),
                separators=(",", ":"),
            ).encode("utf-8")

            # Returns an ordinary JSON error instead of pretending it is an SSE stream.
            return AdapterResponse(
                status_code=provider_response.status_code,
                headers={"Content-Type": "application/json"},
                body=normalized_error,
                streaming=False,
            )

        # Returns a lazy translator that emits OpenAI-compatible SSE events.
        return AdapterResponse(
            status_code=provider_response.status_code,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": provider_response.headers.get(
                    "cache-control",
                    "no-cache",
                ),
            },
            body=translate_anthropic_stream(
                request,
                provider_response,
                provider_client,
            ),
            streaming=True,
        )
