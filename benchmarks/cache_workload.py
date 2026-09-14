"""Measure cache hit rate and estimated provider spend avoided by ProxyLLM."""

# Parses workload size and gateway settings from the command line.
import argparse

# Records when the measured workload ran.
from datetime import datetime, timezone

# Adds exact decimal cost values across many small requests.
from decimal import Decimal, InvalidOperation

# Reads safe endpoint defaults and generates an isolated workload namespace.
import os
import uuid
from time import sleep
from benchmarks.evidence import provenance

# Sends repeated OpenAI-compatible requests to the running gateway.
import httpx

# Loads ignored local values only when the user executes this benchmark.
from dotenv import load_dotenv

# Supplies safe environment lookup and optional report persistence.
from benchmarks.common import require_environment_value, write_json_report


# Builds the cache workload command-line interface.
def build_parser() -> argparse.ArgumentParser:
    # Explains that this tool measures both cache behavior and money avoided.
    parser = argparse.ArgumentParser(
        description="Measure ProxyLLM cache hit rate and estimated spend avoided."
    )
    parser.add_argument('--request-pause', type=float, default=3.0,
                        help='Pause before each request to avoid exhausting the per-key limit.')

    # Controls how many different request keys appear in one workload.
    parser.add_argument("--unique-prompts", type=int, default=10)

    # Controls how many complete rounds repeat those same prompts.
    parser.add_argument("--repeats", type=int, default=5)

    # Selects a cacheable public gateway model.
    parser.add_argument(
        "--model",
        default="fireworks/deepseek-v4-flash",
    )

    # Selects the running gateway endpoint.
    parser.add_argument(
        "--gateway-url",
        default=os.getenv(
            "PROXY_GATEWAY_URL",
            "http://127.0.0.1:8000/v1/chat/completions",
        ),
    )

    # Names the environment variable that holds the app's virtual key.
    parser.add_argument("--gateway-key-env", default="PROXY_VIRTUAL_KEY")

    # Keeps each generated response small during real-provider measurements.
    parser.add_argument("--max-tokens", type=int, default=32)

    # Bounds stalled gateway or provider calls.
    parser.add_argument("--timeout", type=float, default=120.0)

    # Allows the full measurement to be saved for later resume evidence.
    parser.add_argument("--output")

    # Returns the configured parser to the entry point.
    return parser


# Parses one safe decimal response header while treating absence as zero.
def decimal_header(response: httpx.Response, header_name: str) -> Decimal:
    # Reads the gateway-owned metric without looking at response content.
    header_value = response.headers.get(header_name, "0")

    # Converts the decimal text while tolerating malformed intermediary headers.
    try:
        # Preserves very small USD values exactly during aggregation.
        return Decimal(header_value)

    # Keeps one malformed measurement from crashing after provider spend occurred.
    except InvalidOperation:
        # Zero makes missing evidence visible in the final totals.
        return Decimal("0")


# Runs a fresh repeated-query workload and calculates cost reduction metrics.
def run_workload(arguments: argparse.Namespace) -> dict[str, object]:
    # Requires at least one unique prompt and two rounds to measure a real hit.
    if arguments.unique_prompts < 1 or arguments.repeats < 2:
        # Stops before spending provider credit on an invalid workload.
        raise SystemExit("Use --unique-prompts >= 1 and --repeats >= 2.")

    # Loads the gateway's virtual secret without printing it.
    gateway_key = require_environment_value(arguments.gateway_key_env)

    # Makes the first round miss even when older benchmark cache entries still exist.
    workload_id = uuid.uuid4().hex

    # Creates stable distinct prompts that are repeated in later rounds.
    prompts = [
        (
            f"Cache benchmark {workload_id}, item {index}: "
            "reply with the item number only."
        )
        for index in range(arguments.unique_prompts)
    ]

    # Uses the same virtual key and content type for every workload request.
    headers = {
        "Authorization": f"Bearer {gateway_key}",
        "Content-Type": "application/json",
    }

    # Counts observed gateway cache outcomes from explicit response headers.
    hits = 0
    misses = 0
    errors = 0

    # Accumulates actual provider estimate and cache-avoided estimate separately.
    actual_provider_spend = Decimal("0")
    avoided_provider_spend = Decimal("0")

    # Reuses one gateway connection pool across the complete workload.
    with httpx.Client(timeout=arguments.timeout) as client:
        # Repeats the complete prompt set so every later round should hit.
        for _round_number in range(arguments.repeats):
            # Sends each distinct request once per round.
            for prompt in prompts:
                sleep(max(0, getattr(arguments, 'request_pause', 0)))
                # Builds a deterministic complete request with explicit cache consent.
                body = {
                    "model": arguments.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "cache": True,
                    "max_tokens": arguments.max_tokens,
                    "stream": False,
                }

                # Waits for the complete response and its cache measurement headers.
                response = client.post(
                    arguments.gateway_url,
                    headers=headers,
                    json=body,
                )

                # Rejects API errors instead of counting them as misses or savings.
                response.raise_for_status()

                # Reads the gateway's explicit cache source classification.
                cache_status = response.headers.get("X-Proxy-Cache", "UNKNOWN")

                # Counts successful stored response reuse.
                if cache_status == "HIT":
                    # Adds one avoided provider call.
                    hits += 1

                    # Adds the original response's model-price estimate.
                    avoided_provider_spend += decimal_header(
                        response,
                        "X-Proxy-Cost-Avoided-USD",
                    )

                # Counts a successful provider call that populated the cache.
                elif cache_status == "MISS":
                    # Adds one provider-backed cache miss.
                    misses += 1

                    # Adds this response's estimated provider charge.
                    actual_provider_spend += decimal_header(
                        response,
                        "X-Proxy-Estimated-Cost-USD",
                    )

                # Separately counts cache infrastructure or unexpected classifications.
                else:
                    # Avoids overstating either hit rate or avoided spend.
                    errors += 1

                    # Counts the provider cost when a successful call could not cache.
                    actual_provider_spend += decimal_header(
                        response,
                        "X-Proxy-Estimated-Cost-USD",
                    )

    # Includes every successful workload request in the honest hit-rate denominator.
    total_requests = arguments.unique_prompts * arguments.repeats

    # Calculates the request-level cache effectiveness percentage.
    hit_rate_percent = (
        Decimal(hits) / Decimal(total_requests) * Decimal("100")
        if total_requests
        else Decimal("0")
    )

    # Reconstructs the estimated no-cache baseline for this exact workload.
    baseline_provider_spend = actual_provider_spend + avoided_provider_spend

    # Calculates how much estimated provider spend caching removed.
    spend_avoided_percent = (
        avoided_provider_spend
        / baseline_provider_spend
        * Decimal("100")
        if baseline_provider_spend
        else Decimal("0")
    )

    # Returns a secret-free report suitable for long-term comparison.
    return {
        "provenance": provenance(),
        "benchmark": "cache_workload",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "workload_id": workload_id,
        "model": arguments.model,
        "unique_prompts": arguments.unique_prompts,
        "repeats": arguments.repeats,
        "total_requests": total_requests,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_errors": errors,
        "cache_hit_rate_percent": round(float(hit_rate_percent), 3),
        "estimated_actual_provider_spend_usd": format(
            actual_provider_spend,
            "f",
        ),
        "estimated_spend_avoided_usd": format(
            avoided_provider_spend,
            "f",
        ),
        "estimated_no_cache_baseline_usd": format(
            baseline_provider_spend,
            "f",
        ),
        "estimated_spend_avoided_percent": round(
            float(spend_avoided_percent),
            3,
        ),
    }


# Loads local settings, runs the workload, and prints paired effectiveness numbers.
def main() -> None:
    # Loads ignored local values without replacing exported environment settings.
    load_dotenv()

    # Parses and validates command-line configuration.
    arguments = build_parser().parse_args()

    # Sends the complete repeated-query workload to the gateway.
    report = run_workload(arguments)

    # Prints both required values together so hit rate is never shown without savings.
    print(
        "Cache hit rate: "
        f"{report['cache_hit_rate_percent']:.3f}% "
        f"({report['cache_hits']} hits / {report['total_requests']} requests)"
    )
    print(
        "Estimated provider spend avoided: "
        f"{report['estimated_spend_avoided_percent']:.3f}% "
        f"(${report['estimated_spend_avoided_usd']})"
    )

    # Saves full evidence only when the user selected an output path.
    write_json_report(report, arguments.output)


# Runs only when invoked with `python -m benchmarks.cache_workload`.
if __name__ == "__main__":
    # Starts the synchronous measured workload.
    main()
