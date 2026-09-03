"""Provide a cost-free OpenAI-compatible target for benchmark verification."""

# Adds realistic asynchronous provider delay without blocking other requests.
import asyncio

# Reads local delay and failure controls from exported environment variables.
import os

# Supplies stable mock response identity timestamps.
import json
import time

# Generates OpenAI-compatible JSON and SSE HTTP responses.
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


# Parses a non-negative integer environment setting with a safe default.
def integer_setting(name: str, default: int) -> int:
    # Reads configuration without logging its value as request data.
    raw_value = os.getenv(name)

    # Uses the documented default when no override exists.
    if raw_value is None:
        # Returns a stable development setting.
        return default

    # Converts terminal text into an integer while rejecting invalid input early.
    try:
        # Parses the explicit environment override.
        value = int(raw_value)

    # Stops process startup with the exact setting name that needs correction.
    except ValueError as error:
        # Avoids silently running a materially different benchmark.
        raise RuntimeError(f"{name} must be an integer") from error

    # Prevents negative sleeps, output counts, or failure intervals.
    if value < 0:
        # Explains the common validity rule shared by all mock settings.
        raise RuntimeError(f"{name} cannot be negative")

    # Returns the validated local benchmark setting.
    return value


# Reads deterministic mock behavior once when Uvicorn imports the module.
MOCK_DELAY_MS = integer_setting("MOCK_PROVIDER_DELAY_MS", 25)
MOCK_RESPONSE_TOKENS = integer_setting("MOCK_PROVIDER_RESPONSE_TOKENS", 8)
MOCK_FAIL_EVERY = integer_setting("MOCK_PROVIDER_FAIL_EVERY", 0)


# Creates an isolated FastAPI application that never contacts a real provider.
app = FastAPI()


# Holds request count only for deterministic fault injection during local load tests.
request_count = 0


# Returns a coarse stable token estimate sufficient for cost-free tooling tests.
def estimate_prompt_tokens(body: object) -> int:
    # Requires the common request object before inspecting its messages.
    if not isinstance(body, dict):
        # Uses one token for malformed fixtures returned as provider errors elsewhere.
        return 1

    # Reads the OpenAI-compatible message list.
    messages = body.get("messages", [])

    # Counts serialized words from string message content only.
    words = 0
    if isinstance(messages, list):
        # Visits each user or assistant turn without retaining its text.
        for message in messages:
            # Selects only documented message objects.
            if isinstance(message, dict):
                # Reads simple text content used by benchmark scripts.
                content = message.get("content")

                # Adds whitespace-separated words when content is textual.
                if isinstance(content, str):
                    # Produces a deterministic approximate prompt size.
                    words += len(content.split())

    # Ensures usage always contains at least one prompt token.
    return max(words, 1)


# Builds one OpenAI-compatible complete response object.
def build_completion(body: dict[str, object]) -> dict[str, object]:
    # Reads the caller's model so direct and gateway shapes remain comparable.
    model = str(body.get("model", "mock-model"))

    # Calculates stable usage for repeatable cache cost estimates.
    prompt_tokens = estimate_prompt_tokens(body)

    # Returns the minimal successful chat completion required by the project.
    return {
        "id": "chatcmpl-mock-benchmark",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "mock benchmark response",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": MOCK_RESPONSE_TOKENS,
            "total_tokens": prompt_tokens + MOCK_RESPONSE_TOKENS,
        },
    }


# Provides a simple readiness route for shell scripts and local checks.
@app.get("/health")
async def health() -> dict[str, str]:
    # Confirms this process is the synthetic provider, not the gateway.
    return {"status": "mock-provider-ready"}


# Handles both complete and SSE chat completions without requiring a real API key.
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Allows this handler to advance deterministic fault-injection state.
    global request_count

    # Parses the provider-compatible request forwarded by the gateway or benchmark.
    body = await request.json()

    # Rejects non-object fixtures using an OpenAI-compatible provider error envelope.
    if not isinstance(body, dict):
        # Returns a stable client error suitable for gateway normalization tests.
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "The mock request body must be an object.",
                    "type": "invalid_request_error",
                    "code": "invalid_body",
                }
            },
        )

    # Counts this accepted request before applying optional deterministic failures.
    request_count += 1

    # Returns a provider 500 on every configured Nth request during load testing.
    if MOCK_FAIL_EVERY and request_count % MOCK_FAIL_EVERY == 0:
        # Simulates an upstream outage without randomness between repeated runs.
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Synthetic provider outage.",
                    "type": "provider_error",
                    "code": "mock_outage",
                }
            },
        )

    # Builds one deterministic response and normalized usage object.
    completion = build_completion(body)

    # Handles a normal complete response after one realistic provider delay.
    if body.get("stream") is not True:
        # Allows concurrent requests to progress while this synthetic call waits.
        await asyncio.sleep(MOCK_DELAY_MS / 1000)

        # Returns the complete provider-compatible JSON response.
        return completion

    # Produces a lazy SSE stream with delay split across two content events.
    async def stream_body():
        # Divides the configured provider delay across incremental chunks.
        chunk_delay_seconds = MOCK_DELAY_MS / 2000

        # Creates the role and first response text event.
        first_chunk = {
            "id": completion["id"],
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": completion["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "mock "},
                    "finish_reason": None,
                }
            ],
        }

        # Waits asynchronously before revealing the first token group.
        await asyncio.sleep(chunk_delay_seconds)

        # Emits the first event using standard SSE framing.
        yield f"data: {json.dumps(first_chunk)}\n\n".encode("utf-8")

        # Creates the final text and usage-bearing event.
        final_chunk = {
            "id": completion["id"],
            "object": "chat.completion.chunk",
            "created": completion["created"],
            "model": completion["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "benchmark response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": completion["usage"],
        }

        # Waits before the final token group so incremental output is observable.
        await asyncio.sleep(chunk_delay_seconds)

        # Emits final usage and the standard OpenAI completion marker.
        yield f"data: {json.dumps(final_chunk)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    # Returns immediately with a lazy unbuffered SSE response.
    return StreamingResponse(
        stream_body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
