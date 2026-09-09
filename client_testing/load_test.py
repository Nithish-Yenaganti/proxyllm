"""Bounded in-process gateway load experiment; no sockets or paid providers."""
import argparse
import asyncio
from contextlib import ExitStack
from functools import partial
import json
from pathlib import Path
import tempfile
from time import perf_counter
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from auth import database
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key
from api import main as gateway
from api.middleware import VirtualKeyAuthMiddleware
from benchmarks.mock_provider import app as mock_app, MOCK_DELAY_MS
from benchmarks.common import summarize_latencies, write_json_report
from providers.fireworks import FireworksAdapter


async def measure(concurrency, per_client, path):
    keys = []
    for index in range(concurrency):
        key = generate_virtual_key()
        await database.create_virtual_key_record(
            f"load-{index}", get_key_prefix(key), hash_virtual_key(key),
            "fireworks", "default", path,
        )
        keys.append(key)
    app = FastAPI()
    app.add_middleware(VirtualKeyAuthMiddleware, database_path=path)
    app.add_api_route("/v1/chat/completions", gateway.chat_completions, methods=["POST"])
    adapter = FireworksAdapter(client_factory=lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), trust_env=False))
    durations = []
    statuses = {}
    with ExitStack() as stack:
        for name in ("create_usage_log_record", "get_cached_response_record",
                     "get_provider_permission_for_key", "upsert_cached_response_record"):
            stack.enter_context(patch.object(gateway, name,
                partial(getattr(database, name), database_path=path)))
        stack.enter_context(patch.object(gateway, "get_provider_adapter", return_value=adapter))
        stack.enter_context(patch.object(gateway, "PROVIDER_CREDENTIALS", {
            ("fireworks", "default"): {"url": "http://mock/v1/chat/completions", "api_key": "mock-only"}}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway", trust_env=False) as client:
            async def worker(key):
                for _ in range(per_client):
                    start = perf_counter()
                    try:
                        response = await client.post("/v1/chat/completions",
                            headers={"Authorization": "Bearer " + key}, json={
                                "model": "fireworks/deepseek-v4-flash", "cache": False,
                                "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16})
                        code = str(response.status_code)
                    except Exception:
                        code = "exception"
                    statuses[code] = statuses.get(code, 0) + 1
                    if code == "200":
                        durations.append((perf_counter() - start) * 1000)
            start = perf_counter()
            await asyncio.gather(*(worker(key) for key in keys))
            elapsed = perf_counter() - start
    return {"concurrency": concurrency, "requests_per_key": per_client,
            "attempted": concurrency * per_client, "statuses": statuses,
            "successful_requests_per_second": len(durations) / elapsed,
            "elapsed_seconds": elapsed,
            "successful_latency": summarize_latencies(durations) if durations else None}


async def run(levels, per_client):
    results = []
    for level in levels:
        with tempfile.TemporaryDirectory(prefix="proxyllm-load-") as folder:
            path = Path(folder) / "test.db"
            await database.initialize_database(path)
            results.append(await measure(level, per_client, path))
    return {"benchmark": "in_process_mock_load", "mock_delay_ms": MOCK_DELAY_MS,
            "limitations": "No real network, TLS, Uvicorn, or provider latency. Not production capacity. Bounded complete-response batches, not sustained load.",
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", nargs="+", type=int, default=[1, 4, 8])
    parser.add_argument("--requests-per-client", type=int, default=10)
    parser.add_argument("--output", default="benchmarks/results/mock-load.json")
    args = parser.parse_args()
    if any(n < 1 or n > 32 for n in args.levels) or not 1 <= args.requests_per_client <= 24:
        parser.error("Use 1–32 clients and 1–24 requests per client.")
    report = asyncio.run(run(args.levels, args.requests_per_client))
    print(json.dumps(report, indent=2))
    write_json_report(report, args.output)


if __name__ == "__main__":
    main()
