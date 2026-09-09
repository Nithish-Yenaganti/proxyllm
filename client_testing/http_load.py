"""Sustained localhost HTTP smoke load with separate mock and gateway processes."""
import argparse
import asyncio
from contextlib import ExitStack
from functools import partial
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import FastAPI
from auth import database
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key
from benchmarks.common import summarize_latencies, write_json_report


def server(kind, path, port, provider_port):
    if kind == "mock":
        from benchmarks.mock_provider import app
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
        return
    from api import main as gateway
    from api.middleware import VirtualKeyAuthMiddleware
    from providers.fireworks import FireworksAdapter
    app = FastAPI()
    app.add_middleware(VirtualKeyAuthMiddleware, database_path=Path(path))
    app.add_api_route("/v1/chat/completions", gateway.chat_completions, methods=["POST"])
    with ExitStack() as stack:
        for name in ("create_usage_log_record", "get_cached_response_record",
                     "get_provider_permission_for_key", "upsert_cached_response_record"):
            stack.enter_context(patch.object(gateway, name,
                partial(getattr(database, name), database_path=Path(path))))
        stack.enter_context(patch.object(gateway, "get_provider_adapter", return_value=
            FireworksAdapter(client_factory=lambda: httpx.AsyncClient(timeout=10, trust_env=False))))
        stack.enter_context(patch.object(gateway, "PROVIDER_CREDENTIALS", {
            ("fireworks", "default"): {"url": f"http://127.0.0.1:{provider_port}/v1/chat/completions",
                                       "api_key": "mock-only"}}))
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run(clients, seconds):
    processes = []
    with tempfile.TemporaryDirectory(prefix="proxyllm-http-") as directory:
        path = Path(directory) / "test.db"
        await database.initialize_database(path)
        keys = []
        for i in range(clients):
            key = generate_virtual_key()
            await database.create_virtual_key_record(f"http-{i}", get_key_prefix(key),
                hash_virtual_key(key), "fireworks", "default", path)
            keys.append(key)
        mock_port, proxy_port = free_port(), free_port()
        while proxy_port == mock_port:
            proxy_port = free_port()
        try:
            for kind, port in [("mock", mock_port), ("gateway", proxy_port)]:
                processes.append(subprocess.Popen([sys.executable, "-m", "client_testing.http_load",
                    "--serve", kind, "--database", str(path), "--port", str(port),
                    "--provider-port", str(mock_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                for port in (mock_port, proxy_port):
                    for _ in range(100):
                        if any(p.poll() is not None for p in processes):
                            raise RuntimeError("An isolated test server failed to start.")
                        try:
                            await client.get(f"http://127.0.0.1:{port}/")
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("Isolated server startup timed out.")
                statuses, latencies = {}, []
                start = perf_counter()
                deadline = start + seconds
                async def worker(key):
                    while perf_counter() < deadline:
                        began = perf_counter()
                        try:
                            r = await client.post(f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                                headers={"Authorization": "Bearer " + key}, json={
                                    "model": "fireworks/deepseek-v4-flash", "cache": False,
                                    "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16})
                            code = str(r.status_code)
                        except httpx.HTTPError:
                            code = "transport_error"
                        statuses[code] = statuses.get(code, 0) + 1
                        if code == "200":
                            latencies.append((perf_counter() - began) * 1000)
                        # One call per key per three seconds stays below 24/minute.
                        await asyncio.sleep(max(0, min(began + 3, deadline) - perf_counter()))
                await asyncio.gather(*(worker(key) for key in keys))
                elapsed = perf_counter() - start
                return {"benchmark": "paced_local_http", "clients": clients,
                    "duration_seconds": elapsed, "statuses": statuses,
                    "successful_requests_per_second": len(latencies) / elapsed,
                    "successful_latency": summarize_latencies(latencies) if latencies else None,
                    "limitations": "Paced complete-response smoke load, not maximum capacity. Local mock, no TLS or real provider. Three-second per-key interval; real limiter enabled."}
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--output", default="benchmarks/results/local-http-load.json")
    parser.add_argument("--serve", choices=["mock", "gateway"], help=argparse.SUPPRESS)
    parser.add_argument("--database", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--provider-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        server(args.serve, args.database, args.port, args.provider_port)
        return
    if not 1 <= args.clients <= 32 or not 3 <= args.seconds <= 300:
        parser.error("Use 1–32 clients and 3–300 seconds.")
    report = asyncio.run(run(args.clients, args.seconds))
    print(json.dumps(report, indent=2))
    write_json_report(report, args.output)


if __name__ == "__main__":
    main()
