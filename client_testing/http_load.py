"""Sustained localhost HTTP smoke load with separate mock and gateway processes."""
import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager
from functools import partial
import json
import os
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
from benchmarks.evidence import provenance, measure_request


def server(kind, path, port, provider_port, pooled=False):
    if kind == "mock":
        from benchmarks.mock_provider import app
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
        return
    from api import main as gateway
    from api.middleware import VirtualKeyAuthMiddleware
    from providers.fireworks import FireworksAdapter
    @asynccontextmanager
    async def lifespan(app):
        if pooled:
            from providers.connections import pooled_adapters
            async with pooled_adapters() as adapters:
                app.state.provider_adapters = adapters
                yield
        else:
            yield
    app = FastAPI(lifespan=lifespan)
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
        stack.enter_context(patch.object(gateway, "PARAMETER_DROP_POLICY", {}))
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run(clients, seconds, pooled=False, *, interval=3.0, stream=False,
              cache_every=0, delay_ms=25):
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
                    "--provider-port", str(mock_port)] + (["--pooled"] if pooled else []),
                    env={**os.environ, 'MOCK_PROVIDER_DELAY_MS': str(delay_ms),
                         'MOCK_PROVIDER_FAIL_EVERY':'0', 'MOCK_PROVIDER_RESPONSE_TOKENS':'8'},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
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
                statuses, latencies, samples = {}, [], []
                start = perf_counter()
                deadline = start + seconds
                async def worker(key):
                    sequence = 0
                    while perf_counter() < deadline:
                        began = perf_counter()
                        repeated = bool(cache_every and sequence % cache_every == 0)
                        sample = await measure_request(client,
                            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                            {"Authorization": "Bearer " + key}, {
                                "model": "fireworks/deepseek-v4-flash", "cache": bool(cache_every),
                                "stream": stream, "messages": [{"role":"user", "content":
                                "repeat fixture" if repeated else f"unique fixture {sequence}"}], "max_tokens":16})
                        sequence += 1
                        samples.append(sample)
                        code = sample['status']
                        statuses[code] = statuses.get(code, 0) + 1
                        if code == "200" and sample['complete']:
                            latencies.append(sample['total_ms'])
                        # One call per key per three seconds stays below 24/minute.
                        await asyncio.sleep(max(0, min(began + interval, deadline) - perf_counter()))
                await asyncio.gather(*(worker(key) for key in keys))
                elapsed = perf_counter() - start
                ttft = [s['ttft_ms'] for s in samples if s['complete'] and s['ttft_ms'] is not None]
                return {"benchmark": "local_http", "provenance":provenance(),
                    "pooled": pooled, "clients": clients, "interval_seconds":interval,
                    "stream":stream, "cache_every":cache_every, "mock_delay_ms":delay_ms,
                    "provider_concurrency_limit":5, "slot_wait_seconds":2,
                    "rate_limit_per_key":24, "rate_window_seconds":60,
                    "samples": samples, "attempted":len(samples), "successful":len(latencies),
                    "incomplete_200":sum(s['status']=='200' and not s['complete'] for s in samples),
                    "cache_hits":sum(s['cache']=='HIT' for s in samples),
                    "ttft":summarize_latencies(ttft) if ttft else None,
                    "duration_seconds": elapsed, "statuses": statuses,
                    "successful_requests_per_second": len(latencies) / elapsed,
                    "successful_latency": summarize_latencies(latencies) if latencies else None,
                    "limitations": "Local mock, no TLS or real provider; closed-loop workers and per-key rate limiting constrain throughput. TTFT measures first content delta, not headers. Synthetic cache mix is not real application traffic. Includes drain time; no maximum-capacity claim."}
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
    parser.add_argument("--pooled", action="store_true", help="Use application-owned provider connection pools.")
    parser.add_argument('--interval', type=float, default=3)
    parser.add_argument('--stream', action='store_true')
    parser.add_argument('--cache-every', type=int, default=0, help='Repeat one prompt every N requests per key; others are unique (0 disables cache).')
    parser.add_argument('--delay-ms', type=int, default=25)
    parser.add_argument("--serve", choices=["mock", "gateway"], help=argparse.SUPPRESS)
    parser.add_argument("--database", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--provider-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        server(args.serve, args.database, args.port, args.provider_port, args.pooled)
        return
    if not 1 <= args.clients <= 32 or not 3 <= args.seconds <= 300:
        parser.error("Use 1–32 clients and 3–300 seconds.")
    if not 0 <= args.interval <= 60 or not 0 <= args.cache_every <= 100 or not 0 <= args.delay_ms <= 5000:
        parser.error('Invalid interval, cache mix, or mock delay.')
    if args.stream and args.cache_every:
        parser.error('Cache workloads must be non-streaming.')
    report = asyncio.run(run(args.clients, args.seconds, args.pooled,
        interval=args.interval, stream=args.stream, cache_every=args.cache_every, delay_ms=args.delay_ms))
    print(json.dumps(report, indent=2))
    write_json_report(report, args.output)


if __name__ == "__main__":
    main()
