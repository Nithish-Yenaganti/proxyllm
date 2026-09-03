"""Verify benchmark math and reporting without network traffic or provider spend."""

# Builds realistic parsed argument fixtures for workload functions.
import argparse

# Supplies synchronous unit tests and safe temporary replacement helpers.
import unittest
from unittest.mock import patch

# Supplies minimal response request metadata used by HTTPX errors.
import httpx

# Exercises cache workload aggregation, percentile math, and ceiling selection.
from benchmarks.cache_workload import run_workload
from benchmarks.common import percentile, summarize_latencies
from benchmarks.load_sweep import find_ceiling, parse_levels


# Returns deterministic cache outcomes without opening an HTTP connection.
class FakeGatewayClient:
    # Matches the HTTPX constructor while accepting its unused timeout argument.
    def __init__(self, *, timeout: float) -> None:
        # Saves the timeout only to prove construction accepted benchmark settings.
        self.timeout = timeout

        # Counts calls so the first prompt round misses and later rounds hit.
        self.request_count = 0

    # Supports the context-manager protocol used by the real benchmark.
    def __enter__(self):
        # Returns this in-memory client to the workload loop.
        return self

    # Performs no cleanup because this fake owns no sockets.
    def __exit__(self, _error_type, _error, _traceback) -> None:
        # Explicitly returns None so exceptions would not be suppressed.
        return None

    # Returns two misses followed by four hits for a two-prompt, three-round workload.
    def post(self, url: str, *, headers, json) -> httpx.Response:
        # Counts this request without storing its secret Authorization header.
        self.request_count += 1

        # Selects the expected cache result based on workload order.
        cache_status = "MISS" if self.request_count <= 2 else "HIT"

        # Supplies actual cost for misses and avoided cost for hits.
        response_headers = {
            "X-Proxy-Cache": cache_status,
            (
                "X-Proxy-Estimated-Cost-USD"
                if cache_status == "MISS"
                else "X-Proxy-Cost-Avoided-USD"
            ): "0.01",
        }

        # Returns a successful response with a request attached for raise_for_status().
        return httpx.Response(
            200,
            headers=response_headers,
            json={"ok": True},
            request=httpx.Request("POST", url),
        )


# Exercises the shared statistics used by the latency benchmark.
class BenchmarkMathTests(unittest.TestCase):
    # Confirms interpolated percentiles work for small deterministic samples.
    def test_percentiles_and_latency_summary(self) -> None:
        # Uses a deliberately unsorted sample to prove the helper sorts a copy.
        samples = [40.0, 10.0, 30.0, 20.0]

        # Confirms the midpoint interpolation used for p50.
        self.assertEqual(percentile(samples, 50), 25.0)

        # Confirms all required interview metrics are present.
        summary = summarize_latencies(samples)
        self.assertEqual(summary["p50_ms"], 25.0)
        self.assertIn("p95_ms", summary)
        self.assertIn("p99_ms", summary)

    # Confirms load-level parsing rejects invalid or empty sweeps.
    def test_load_levels_are_positive_sorted_and_unique(self) -> None:
        # Normalizes duplicate unordered terminal input.
        self.assertEqual(parse_levels("50,10,25,10"), [10, 25, 50])

        # Rejects a level that could not create any virtual user.
        with self.assertRaises(argparse.ArgumentTypeError):
            # Supplies the invalid zero concurrency point.
            parse_levels("0,10")

    # Confirms the ceiling is the highest measured point satisfying both limits.
    def test_load_ceiling_uses_latency_and_error_rate(self) -> None:
        # Creates one healthy point and two differently degraded points.
        results = [
            {
                "configured_peak_vus": 10,
                "latency_ms": {"p95": 900},
                "error_rate": 0,
            },
            {
                "configured_peak_vus": 25,
                "latency_ms": {"p95": 1800},
                "error_rate": 0.005,
            },
            {
                "configured_peak_vus": 50,
                "latency_ms": {"p95": 2300},
                "error_rate": 0.02,
            },
        ]

        # Selects only the highest actually tested healthy concurrency.
        ceiling = find_ceiling(results, 2000, 0.01)
        self.assertIsNotNone(ceiling)
        assert ceiling is not None
        self.assertEqual(ceiling["configured_peak_vus"], 25)


# Exercises workload cost arithmetic through a fake HTTPX client.
class CacheWorkloadTests(unittest.TestCase):
    # Confirms hit rate and avoided-spend percentage are reported together correctly.
    def test_cache_workload_reports_hits_and_spend_avoided(self) -> None:
        # Builds the smallest repeated workload that has two misses and four hits.
        arguments = argparse.Namespace(
            unique_prompts=2,
            repeats=3,
            model="fireworks/deepseek-v4-flash",
            gateway_url="http://gateway.test/v1/chat/completions",
            gateway_key_env="PROXY_VIRTUAL_KEY",
            max_tokens=8,
            timeout=10.0,
            output=None,
        )

        # Replaces secret lookup and network traffic with deterministic local fixtures.
        with (
            patch(
                "benchmarks.cache_workload.require_environment_value",
                return_value="test-virtual-key",
            ),
            patch(
                "benchmarks.cache_workload.httpx.Client",
                FakeGatewayClient,
            ),
        ):
            # Runs all aggregation code without opening a socket.
            report = run_workload(arguments)

        # Confirms the measured request outcomes.
        self.assertEqual(report["cache_hits"], 4)
        self.assertEqual(report["cache_misses"], 2)
        self.assertEqual(report["cache_hit_rate_percent"], 66.667)

        # Confirms avoided spend is compared with the reconstructed no-cache baseline.
        self.assertEqual(report["estimated_actual_provider_spend_usd"], "0.02")
        self.assertEqual(report["estimated_spend_avoided_usd"], "0.04")
        self.assertEqual(report["estimated_spend_avoided_percent"], 66.667)


# Runs this module directly with normal unittest output.
if __name__ == "__main__":
    # Starts every benchmark math test.
    unittest.main()
