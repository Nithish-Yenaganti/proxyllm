"""Read a bounded JSON request without trusting Content-Length."""
import asyncio
import json
import math
from starlette.requests import Request


DEFAULT_BODY_TIMEOUT_SECONDS = 30.0


class RequestBodyTimeout(Exception):
    pass


class RequestTooLarge(Exception):
    pass


def configured_body_limit(value: str) -> int:
    limit = int(value)
    if limit <= 0:
        raise ValueError("PROXY_MAX_REQUEST_BYTES must be a positive integer")
    return limit


def configured_body_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PROXY_REQUEST_BODY_TIMEOUT_SECONDS must be finite and positive")
    return timeout


def validate_json_values(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 128:
            raise ValueError("JSON nesting exceeds 128 levels")
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON numbers must be finite")
        elif isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item.keys())
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


async def read_json_body(request: Request, limit: int,
                         timeout: float = DEFAULT_BODY_TIMEOUT_SECONDS):
    # A declared oversized body can be rejected without reading it. Actual bytes
    # are always counted too, including when the header is absent or understated.
    declared = request.headers.get("content-length", "")
    if (declared.isascii() and declared.isdecimal()
            and (len(declared.lstrip("0")) > len(str(limit))
                 or int(declared.lstrip("0") or "0") > limit)):
        raise RequestTooLarge
    body = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise RequestTooLarge
                body.extend(chunk)
    except TimeoutError:
        raise RequestBodyTimeout from None
    value = json.loads(body)
    validate_json_values(value)
    return value
