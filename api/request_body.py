"""Read a bounded JSON request without trusting Content-Length."""
import json
from starlette.requests import Request


class RequestTooLarge(Exception):
    pass


def configured_body_limit(value: str) -> int:
    limit = int(value)
    if limit <= 0:
        raise ValueError("PROXY_MAX_REQUEST_BYTES must be a positive integer")
    return limit


async def read_json_body(request: Request, limit: int):
    # A declared oversized body can be rejected without reading it. Actual bytes
    # are always counted too, including when the header is absent or understated.
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdecimal() and int(declared) > limit:
        raise RequestTooLarge
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestTooLarge
        body.extend(chunk)
    return json.loads(body)
