"""Exercise a running proxy without printing credentials or response content."""
import argparse
import asyncio
import json
import os
import uuid

import httpx
from client_testing.environment import load_client_environment


async def check(args):
    key = os.environ.get("PROXY_VIRTUAL_KEY")
    second = os.environ.get("PROXY_SECOND_VIRTUAL_KEY")
    if not key:
        raise SystemExit("Set PROXY_VIRTUAL_KEY.")
    if args.mode == "isolation" and (not second or second == key):
        raise SystemExit("Set PROXY_SECOND_VIRTUAL_KEY to a different key.")
    body = {"model": args.model, "messages": [{"role": "user", "content":
        "Reply with hello. Test " + uuid.uuid4().hex}], "max_tokens": 16,
        "cache": False}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60) as client:
        async def send(token, payload):
            return await client.post("chat/completions", json=payload,
                                     headers={"Authorization": "Bearer " + token})

        if args.mode == "rate-limit":
            # Invalid input consumes authenticated admission but cannot call a provider.
            codes = [(await send(key, {})).status_code for _ in range(25)]
            if codes != [400] * 24 + [429]:
                raise RuntimeError("Expected 24 validation errors followed by throttling; use a fresh key.")
        elif args.mode == "denied":
            if (await send(key, body)).status_code != 403:
                raise RuntimeError("Expected denied provider permission.")
        elif args.mode == "stream":
            body["stream"] = True
            done = False
            async with client.stream("POST", "chat/completions", json=body,
                                     headers={"Authorization": "Bearer " + key}) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"Unexpected HTTP status {response.status_code}")
                async for line in response.aiter_lines():
                    if line == "data: [DONE]":
                        done = True
            if not done:
                raise RuntimeError("Stream ended without completion marker.")
        else:
            body["cache"] = args.mode in {"cache", "isolation"}
            tokens = [key, key] if args.mode == "cache" else (
                [key, key, second, second] if args.mode == "isolation" else [key])
            statuses = []
            for token in tokens:
                response = await send(token, body)
                if response.status_code != 200:
                    raise RuntimeError(f"Unexpected HTTP status {response.status_code}")
                if not response.json().get("choices"):
                    raise RuntimeError("Response has no choices.")
                statuses.append(response.headers.get("X-Proxy-Cache"))
            expected = {"cache": ["MISS", "HIT"],
                        "isolation": ["MISS", "HIT", "MISS", "HIT"]}
            if args.mode in expected and statuses != expected[args.mode]:
                raise RuntimeError("Unexpected cache behavior.")
    print(json.dumps({"check": args.mode, "passed": True}))


def main():
    load_client_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["chat", "stream", "cache", "isolation", "denied", "rate-limit"])
    parser.add_argument("--model", required=True, help="Configured public model name.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1/")
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/") + "/"
    try:
        asyncio.run(check(args))
    except (RuntimeError, httpx.HTTPError, ValueError) as error:
        # Do not print HTTP exception URLs, response bodies, or request headers.
        raise SystemExit("Client check failed: " + (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__)) from None


if __name__ == "__main__":
    main()
