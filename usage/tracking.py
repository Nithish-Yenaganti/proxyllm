"""Extract token usage, estimate cost, and observe streamed responses."""

# Shields the final SQLite write when a streaming response is cancelled.
import asyncio

# Supplies asynchronous byte iterators and completion callback annotations.
from collections.abc import AsyncIterator, Awaitable, Callable

# Holds compact token totals without passing provider response objects around.
from dataclasses import dataclass

# Calculates small USD values without binary floating-point rounding surprises.
from decimal import Decimal

# Parses complete response JSON and OpenAI-compatible SSE data events.
import json

# Incrementally decodes compressed Fireworks streams for observation only.
import zlib


# Represents one provider response's normalized token counters.
@dataclass
class TokenUsage:
    # Counts all input tokens reported in the public OpenAI-compatible response.
    prompt_tokens: int = 0

    # Counts generated output tokens reported by the provider.
    completion_tokens: int = 0

    # Counts the complete input-plus-output total.
    total_tokens: int = 0

    # Counts cached input included inside prompt_tokens when the provider reports it.
    cached_prompt_tokens: int = 0


# Represents the final facts collected while an SSE response passes through unchanged.
@dataclass
class StreamObservation:
    # Stores the latest usage object observed in a streamed data event.
    usage: TokenUsage

    # Confirms that the provider emitted OpenAI's terminal stream marker.
    completed: bool = False

    # Records a normalized provider error event inside an HTTP 200 stream.
    provider_error: bool = False


# Returns a safe non-negative integer or zero for malformed provider values.
def safe_token_count(value: object) -> int:
    # Excludes booleans because Python treats them as integers.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        # Preserves a valid provider token counter.
        return value

    # Prevents malformed usage metadata from breaking the client response.
    return 0


# Extracts OpenAI-compatible token counters from one decoded response object.
def extract_token_usage(payload: object) -> TokenUsage:
    # Reads usage only from a documented top-level mapping.
    usage = payload.get("usage") if isinstance(payload, dict) else None

    # Treats errors and responses without usage as zero-token observations.
    if not isinstance(usage, dict):
        # Returns a consistent empty counter object for logging.
        return TokenUsage()

    # Reads the three common OpenAI-compatible totals independently.
    prompt_tokens = safe_token_count(usage.get("prompt_tokens"))
    completion_tokens = safe_token_count(usage.get("completion_tokens"))
    supplied_total = safe_token_count(usage.get("total_tokens"))

    # Uses the component sum when a provider omitted its redundant total field.
    total_tokens = supplied_total or prompt_tokens + completion_tokens

    # Reads optional cached-token detail produced by Fireworks and similar providers.
    prompt_details = usage.get("prompt_tokens_details")

    # Extracts the cached subset only from a valid details object.
    cached_prompt_tokens = (
        safe_token_count(prompt_details.get("cached_tokens"))
        if isinstance(prompt_details, dict)
        else 0
    )

    # Returns provider-independent counters ready for cost calculation and storage.
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_prompt_tokens=min(cached_prompt_tokens, prompt_tokens),
    )


# Parses token usage from one complete JSON response body.
def extract_token_usage_from_body(body: bytes) -> TokenUsage:
    # Converts UTF-8 JSON into a Python value without trusting its shape.
    try:
        # Passes the decoded response to the shared usage extractor.
        return extract_token_usage(json.loads(body))

    # Treats provider errors and malformed successful bodies as unknown usage.
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Keeps observability failure from changing the client-visible response.
        return TokenUsage()


# Calculates the estimated standard-provider charge for one request.
def estimate_cost_usd(
    usage: TokenUsage,
    input_cost_per_million: Decimal,
    output_cost_per_million: Decimal,
    cached_input_cost_per_million: Decimal,
) -> Decimal:
    # Separates cached input from ordinary input so discounts are not double-counted.
    uncached_prompt_tokens = max(
        usage.prompt_tokens - usage.cached_prompt_tokens,
        0,
    )

    # Prices each token category using its model-route rate snapshot.
    total = (
        Decimal(uncached_prompt_tokens) * input_cost_per_million
        + Decimal(usage.cached_prompt_tokens) * cached_input_cost_per_million
        + Decimal(usage.completion_tokens) * output_cost_per_million
    ) / Decimal("1000000")

    # Stores up to twelve decimal places so tiny development calls remain measurable.
    return total.quantize(Decimal("0.000000000001"))


# Incrementally reads usage events while leaving streamed bytes unchanged for the client.
class OpenAIStreamObserver:
    # Configures optional decompression based on the forwarded Content-Encoding header.
    def __init__(self, content_encoding: str | None = None) -> None:
        # Holds incomplete SSE lines split across arbitrary network chunks.
        self.buffer = b""

        # Exposes the final facts used when the stream closes.
        self.observation = StreamObservation(usage=TokenUsage())

        # Normalizes the optional encoding name for matching.
        normalized_encoding = (content_encoding or "").lower()

        # Creates a gzip decoder that observes a copy while original bytes pass through.
        if normalized_encoding == "gzip":
            # Uses the gzip wrapper mode required by HTTP Content-Encoding.
            self.decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)

        # Creates a standard zlib decoder for deflate-encoded response streams.
        elif normalized_encoding == "deflate":
            # Uses zlib's default wrapped DEFLATE format.
            self.decompressor = zlib.decompressobj()

        # Leaves uncompressed and unsupported encodings untouched.
        else:
            # None tells feed() that chunks already contain readable SSE bytes.
            self.decompressor = None

    # Observes one raw network chunk without buffering or changing downstream output.
    def feed(self, chunk: bytes) -> None:
        # Attempts to decode a compressed observation copy when necessary.
        try:
            # Keeps ordinary SSE bytes unchanged for parsing.
            decoded_chunk = (
                self.decompressor.decompress(chunk)
                if self.decompressor is not None
                else chunk
            )

        # Ignores malformed compression metadata rather than breaking the response.
        except zlib.error:
            # Clears pending parser data because it can no longer form valid events.
            self.buffer = b""

            # Stops observing this unusable chunk.
            return

        # Adds only the small unprocessed tail to this newly decoded chunk.
        self.buffer += decoded_chunk

        # Extracts every complete SSE line now available.
        while b"\n" in self.buffer:
            # Keeps any incomplete final line for the next network chunk.
            line, self.buffer = self.buffer.split(b"\n", 1)

            # Parses this complete line independently of SSE chunk boundaries.
            self._observe_line(line.rstrip(b"\r"))

    # Updates stream facts from one complete OpenAI-compatible SSE line.
    def _observe_line(self, line: bytes) -> None:
        # Ignores SSE event names, comments, separators, and other non-data fields.
        if not line.startswith(b"data:"):
            # Waits for a data-bearing line.
            return

        # Removes the field name and optional whitespace from the serialized value.
        serialized_payload = line.removeprefix(b"data:").strip()

        # Recognizes the normal OpenAI stream completion marker.
        if serialized_payload == b"[DONE]":
            # Distinguishes complete streams from disconnects or truncated responses.
            self.observation.completed = True

            # No JSON follows inside this terminal marker.
            return

        # Parses one JSON event without exposing failures to the response pipeline.
        try:
            # Converts the UTF-8 JSON event into a Python value.
            event = json.loads(serialized_payload)

        # Ignores invalid or non-UTF-8 diagnostic events safely.
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Continues observing later stream events.
            return

        # Detects the normalized error envelope used by both adapters.
        if isinstance(event, dict) and isinstance(event.get("error"), dict):
            # Records that an HTTP 200 stream ended with a provider-side failure.
            self.observation.provider_error = True

        # Replaces counters only when this event actually includes a usage object.
        if isinstance(event, dict) and isinstance(event.get("usage"), dict):
            # Keeps the latest cumulative usage normally supplied by the final event.
            self.observation.usage = extract_token_usage(event)


# Passes stream chunks through immediately and writes one log when iteration ends.
async def observe_stream(
    body: AsyncIterator[bytes],
    observer: OpenAIStreamObserver,
    on_finished: Callable[[StreamObservation, bool], Awaitable[None]],
) -> AsyncIterator[bytes]:
    # Tracks whether an iterator exception interrupted normal provider output.
    stream_failed = False

    # Guarantees that successful, failed, cancelled, and disconnected streams are logged.
    try:
        # Pulls each upstream chunk only when the downstream client is ready for it.
        async for chunk in body:
            # Reads metrics from a copy without modifying the bytes sent to the client.
            observer.feed(chunk)

            # Preserves Phase 3 token-by-token forwarding behavior.
            yield chunk

    # Records an exceptional stream outcome before allowing FastAPI to handle it.
    except BaseException:
        # Marks exceptions and task cancellation differently from a clean iterator end.
        stream_failed = True

        # Preserves the original exception and cancellation semantics.
        raise

    # Runs after normal completion, provider failure, or downstream cancellation.
    finally:
        # Starts a separate task so cancellation cannot skip the final SQLite insert.
        logging_task = asyncio.create_task(
            on_finished(observer.observation, stream_failed)
        )

        # Prevents outer cancellation from cancelling the database write task.
        try:
            # Waits normally when the downstream connection is still active.
            await asyncio.shield(logging_task)

        # Allows the response task to remain cancelled while the shielded insert finishes.
        except asyncio.CancelledError:
            # The independently scheduled database operation continues in the event loop.
            pass
