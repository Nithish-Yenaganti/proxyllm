"""Measure direct-provider latency against the same call through ProxyLLM."""

# Parses repeatable benchmark settings from the command line.
import argparse

# Records when the benchmark ran for later comparison.
from datetime import datetime, timezone

# Reads endpoint defaults and secret variable names from the environment.
import os

# Measures complete HTTP response duration with a monotonic clock.
from time import perf_counter, sleep
from benchmarks.evidence import provenance

# Sends pooled direct and gateway requests under the same local process conditions.
import httpx

# Loads ignored local benchmark variables when the user runs this script.
from dotenv import load_dotenv

# Supplies shared secret lookup, percentile calculation, and report persistence.
from benchmarks.common import (
    require_environment_value,
    summarize_latencies,
    write_json_report,
)


# Builds the benchmark command-line interface without accepting secrets as arguments.
def build_parser() -> argparse.ArgumentParser:
    # Describes the comparison shown by terminal help.
    parser = argparse.ArgumentParser(
        description="Compare direct Fireworks latency with ProxyLLM latency."
    )

    # Controls the measured sample size after unreported warm-up calls.
    parser.add_argument("--requests", type=int, default=20)

    # Controls connection and model warm-up pairs before timing begins.
    parser.add_argument("--warmups", type=int, default=2)

    # Selects the identical provider model accepted by both benchmark paths.
    parser.add_argument(
        "--model",
        default="accounts/fireworks/models/deepseek-v4-flash-0731",
    )

    # Selects the real provider endpoint or the optional local mock endpoint.
    parser.add_argument(
        "--direct-url",
        default=os.getenv(
            "FIREWORK_URL",
            "https://api.fireworks.ai/inference/v1/chat/completions",
        ),
    )

    # Selects the already-running local or deployed gateway endpoint.
    parser.add_argument(
        "--gateway-url",
        default=os.getenv(
            "PROXY_GATEWAY_URL",
            "http://127.0.0.1:8000/v1/chat/completions",
        ),
    )

    # Names the environment variables holding provider and virtual secrets.
    parser.add_argument("--provider-key-env", default="FIREWORK_API_KEY")
    parser.add_argument("--gateway-key-env", default="PROXY_VIRTUAL_KEY")

    # Keeps responses inexpensive while still timing a complete provider round trip.
    parser.add_argument("--max-tokens", type=int, default=32)

    # Bounds stalled provider or gateway calls independently.
    parser.add_argument("--timeout", type=float, default=120.0)

    # Allows a versioned local report without requiring one for quick runs.
    parser.add_argument("--output")
    parser.add_argument("--pair-pause", type=float, default=3.0,
                        help="Pause between pairs to leave room below the per-key rate limit.")

    # Returns the configured parser to the entry point.
    return parser


# Times one complete response body and raises on a non-success status.
def time_request(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, object],
) -> float:
    # Starts immediately before the HTTP client begins the request.
    started_at = perf_counter()

    # Waits for headers and the complete non-streaming response body.
    response = client.post(url, headers=headers, json=body)

    # Stops immediately after HTTPX has received the full response.
    duration_ms = (perf_counter() - started_at) * 1000

    # Prevents error responses from being misreported as fast successful calls.
    response.raise_for_status()

    # Returns milliseconds for percentile aggregation.
    return duration_ms


# Runs warmups and alternating paired measurements using two persistent connections.
def run_benchmark(arguments: argparse.Namespace) -> dict[str, object]:
    # Rejects sample sizes that cannot produce meaningful percentile output.
    if arguments.requests < 1 or arguments.warmups < 0 or getattr(arguments, 'pair_pause', 3) < 0:
        # Stops before any billable request is made.
        raise SystemExit("--requests must be positive and --warmups cannot be negative.")

    # Loads the real provider key without displaying it.
    provider_key = require_environment_value(arguments.provider_key_env)

    # Loads the independently issued ProxyLLM virtual key without displaying it.
    gateway_key = require_environment_value(arguments.gateway_key_env)

    # Builds the same provider-compatible request body for both network paths.
    request_body: dict[str, object] = {
        "model": arguments.model,
        "messages": [
            {
                "role": "user",
                "content": "Reply with exactly: latency benchmark",
            }
        ],
        "temperature": 0.2,
        "max_tokens": arguments.max_tokens,
        "stream": False,
    }

    # Uses each path's correct secret while keeping all other headers identical.
    direct_headers = {
        "Authorization": f"Bearer {provider_key}",
        "Content-Type": "application/json",
    }
    gateway_headers = {
        "Authorization": f"Bearer {gateway_key}",
        "Content-Type": "application/json",
    }

    # Holds individual samples so later analysis can verify percentile calculations.
    direct_samples: list[float] = []
    gateway_samples: list[float] = []

    # Reuses connections independently so neither path pays setup on every request.
    with (
        httpx.Client(timeout=arguments.timeout) as direct_client,
        httpx.Client(timeout=arguments.timeout) as gateway_client,
    ):
        # Warms DNS, TLS, connection pools, gateway imports, and model serving.
        for _ in range(arguments.warmups):
            sleep(getattr(arguments, 'pair_pause', 3))
            # Runs the pair back to back without including it in reported numbers.
            time_request(
                direct_client,
                arguments.direct_url,
                direct_headers,
                request_body,
            )
            time_request(
                gateway_client,
                arguments.gateway_url,
                gateway_headers,
                request_body,
            )

        # Alternates pair order to reduce systematic first-or-second request bias.
        for index in range(arguments.requests):
            sleep(getattr(arguments, 'pair_pause', 3))
            # Runs direct first on even iterations.
            if index % 2 == 0:
                # Records provider baseline then gateway measurement.
                direct_samples.append(
                    time_request(
                        direct_client,
                        arguments.direct_url,
                        direct_headers,
                        request_body,
                    )
                )
                gateway_samples.append(
                    time_request(
                        gateway_client,
                        arguments.gateway_url,
                        gateway_headers,
                        request_body,
                    )
                )

            # Runs gateway first on odd iterations to balance short-term network changes.
            else:
                # Records gateway measurement then provider baseline.
                gateway_samples.append(
                    time_request(
                        gateway_client,
                        arguments.gateway_url,
                        gateway_headers,
                        request_body,
                    )
                )
                direct_samples.append(
                    time_request(
                        direct_client,
                        arguments.direct_url,
                        direct_headers,
                        request_body,
                    )
                )

    # Calculates the required tail percentiles independently for each path.
    direct_summary = summarize_latencies(direct_samples)
    gateway_summary = summarize_latencies(gateway_samples)

    # Reports gateway minus direct so a positive value means added overhead.
    overhead_summary = {
        percentile_name: round(
            gateway_summary[percentile_name] - direct_summary[percentile_name],
            3,
        )
        for percentile_name in ("p50_ms", "p95_ms", "p99_ms")
    }

    # Builds a reproducible report without any secret header values.
    return {
        "benchmark": "latency_overhead",
        "provenance": provenance(),
        "pair_pause_seconds": getattr(arguments, 'pair_pause', 3),
        "paired_delta_ms": [round(g-d, 3) for d, g in zip(direct_samples, gateway_samples)],
        "limitations": "Percentile differences are not paired-delta percentiles; provider/network variation can produce negative differences. Small samples do not establish tail latency or a speedup.",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "requests_per_path": arguments.requests,
        "warmups_per_path": arguments.warmups,
        "model": arguments.model,
        "direct_url": arguments.direct_url,
        "gateway_url": arguments.gateway_url,
        "direct": direct_summary,
        "gateway": gateway_summary,
        "gateway_overhead": overhead_summary,
        "raw_samples_ms": {
            "direct": [round(value, 3) for value in direct_samples],
            "gateway": [round(value, 3) for value in gateway_samples],
        },
    }


# Loads configuration, executes the benchmark, and prints resume-ready metrics.
def main() -> None:
    # Loads ignored local values without overriding explicitly exported variables.
    load_dotenv()

    # Converts terminal arguments into validated benchmark configuration.
    arguments = build_parser().parse_args()

    # Runs every billable request only after all required configuration exists.
    report = run_benchmark(arguments)

    # Prints one compact comparison table in milliseconds.
    print("metric | direct_ms | gateway_ms | overhead_ms")
    for metric in ("p50_ms", "p95_ms", "p99_ms"):
        # Displays matching percentiles on one line for easy interview notes.
        print(
            f"{metric.removesuffix('_ms')} | "
            f"{report['direct'][metric]:.3f} | "
            f"{report['gateway'][metric]:.3f} | "
            f"{report['gateway_overhead'][metric]:+.3f}"
        )

    # Writes the complete samples only when the user selected an artifact path.
    write_json_report(report, arguments.output)


# Runs only when invoked with `python -m benchmarks.latency_overhead`.
if __name__ == "__main__":
    # Starts the synchronous paired benchmark.
    main()
