"""Decide cache eligibility and build stable per-application cache keys."""

# Creates a one-way fixed-size key without storing prompts in SQLite indexes.
import hashlib

# Produces canonical JSON independent of dictionary insertion order.
import json

# Supplies JSON-compatible request dictionary annotations.
from typing import Any


# Names the gateway-only request field used for explicit cache control.
CACHE_CONTROL_FIELD = "cache"


# Returns whether one request may safely use complete-response caching.
def is_cache_eligible(body: dict[str, Any]) -> bool:
    # Streaming responses stay live and are never replayed from the response cache.
    if body.get("stream") is True:
        # Avoids buffering SSE or pretending cached text arrived token by token.
        return False

    # Reads an optional explicit application decision before temperature defaults.
    cache_control = body.get(CACHE_CONTROL_FIELD)

    # Lets an application disable caching even for deterministic requests.
    if cache_control is False:
        # Treats explicit opt-out as authoritative.
        return False

    # Lets an application intentionally cache a request with any sampling settings.
    if cache_control is True:
        # The caller accepts the semantic tradeoff for this exact normalized request.
        return True

    # Reads the common deterministic sampling value without treating booleans as zero.
    temperature = body.get("temperature")

    # Automatically caches only an explicitly numeric zero temperature.
    return (
        isinstance(temperature, (int, float))
        and not isinstance(temperature, bool)
        and temperature == 0
    )


# Removes fields understood only by ProxyLLM before provider translation begins.
def build_provider_body(body: dict[str, Any]) -> dict[str, Any]:
    # Copies the request so gateway normalization cannot mutate FastAPI's parsed body.
    provider_body = dict(body)

    # Prevents Fireworks or Anthropic from rejecting the private cache-control field.
    provider_body.pop(CACHE_CONTROL_FIELD, None)

    # Returns the provider-safe unified request used by adapters and cache hashing.
    return provider_body


# Builds a stable privacy-scoped key from the complete provider-relevant request.
def build_cache_key(
    virtual_key_id: int,
    provider_body: dict[str, Any],
) -> str:
    # Normalizes equivalent object key order while preserving arrays and text exactly.
    normalized_request = json.dumps(
        provider_body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    # Includes the owning virtual-key ID to prevent responses crossing app boundaries.
    scoped_request = f"{virtual_key_id}:{normalized_request}"

    # Encodes Unicode deterministically before one-way hashing.
    encoded_request = scoped_request.encode("utf-8")

    # Returns a compact index key without retaining prompts or messages in the schema.
    return hashlib.sha256(encoded_request).hexdigest()
