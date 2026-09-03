"""Run k6 at increasing concurrency and identify the measured healthy ceiling."""

# Parses load levels, thresholds, and output settings from the terminal.
import argparse

# Records one timestamp for the combined sweep report.
from datetime import datetime, timezone

# Parses the compact JSON summary emitted by the project's k6 script.
import json

# Passes secrets and run configuration to k6 without placing them in arguments.
import os

# Resolves script and per-run result paths independent of terminal location.
from pathlib import Path

# Finds and executes the external k6 load-testing binary safely without a shell.
import shutil
import subprocess

# Supplies flexible summary annotations.
from typing import Any

# Loads ignored local secrets only when the user explicitly runs this tool.
from dotenv import load_dotenv

# Writes the optional combined evidence file.
from benchmarks.common import require_environment_value, write_json_report


# Resolves benchmark files relative to this module rather than the current directory.
BENCHMARK_DIRECTORY = Path(__file__).resolve().parent
K6_SCRIPT_PATH = BENCHMARK_DIRECTORY / "load_test.js"
RESULTS_DIRECTORY = BENCHMARK_DIRECTORY / "results"


# Converts a comma-separated concurrency list into unique increasing integers.
def parse_levels(raw_levels: str) -> list[int]:
    # Converts each non-empty terminal segment independently.
    try:
        # Sorts and deduplicates so ceiling selection remains deterministic.
        levels = sorted(
            {
                int(part.strip())
                for part in raw_levels.split(",")
                if part.strip()
            }
        )

    # Replaces Python conversion details with a user-facing command correction.
    except ValueError as error:
        # Names the expected format without starting any load test.
        raise argparse.ArgumentTypeError(
            "levels must be comma-separated positive integers"
        ) from error

    # Requires at least one valid concurrency point.
    if not levels or any(level < 1 for level in levels):
        # Prevents meaningless or negative virtual-user stages.
        raise argparse.ArgumentTypeError(
            "levels must be comma-separated positive integers"
        )

    # Returns the safe ascending sweep configuration.
    return levels


# Builds the wrapper command-line interface around the checked-in k6 test.
def build_parser() -> argparse.ArgumentParser:
    # Describes the measured ceiling rather than promising a fixed production number.
    parser = argparse.ArgumentParser(
        description="Sweep k6 concurrency and report the highest healthy level."
    )

    # Selects increasing virtual-user levels tested in independent runs.
    parser.add_argument(
        "--levels",
        type=parse_levels,
        default=parse_levels("10,25,50,100"),
    )

    # Controls each of the four internal ramp stages at one peak level.
    parser.add_argument("--stage-duration", default="10s")

    # Defines the latency degradation boundary used to call a run healthy.
    parser.add_argument("--p95-limit-ms", type=int, default=2000)

    # Defines the maximum tolerated fraction of failed HTTP requests.
    parser.add_argument("--error-rate-limit", type=float, default=0.01)

    # Selects complete, streaming, or both response modes.
    parser.add_argument(
        "--mode",
        choices=("complete", "streaming", "both"),
        default="both",
    )

    # Allows a combined long-term report outside the ignored per-run files.
    parser.add_argument("--output")

    # Returns the completed parser to the entry point.
    return parser


# Marks one k6 result healthy only when both agreed thresholds remain satisfied.
def is_healthy(
    result: dict[str, Any],
    p95_limit_ms: float,
    error_rate_limit: float,
) -> bool:
    # Reads measured p95 from the compact k6 result shape.
    latency = result.get("latency_ms", {})
    p95 = latency.get("p95") if isinstance(latency, dict) else None

    # Reads the fraction of requests whose HTTP transport or status failed.
    error_rate = result.get("error_rate")

    # Requires numeric evidence for both dimensions before declaring success.
    return (
        isinstance(p95, (int, float))
        and isinstance(error_rate, (int, float))
        and p95 < p95_limit_ms
        and error_rate < error_rate_limit
    )


# Returns the highest tested healthy result without extrapolating beyond evidence.
def find_ceiling(
    results: list[dict[str, Any]],
    p95_limit_ms: float,
    error_rate_limit: float,
) -> dict[str, Any] | None:
    # Keeps only runs satisfying both reliability and latency boundaries.
    healthy_results = [
        result
        for result in results
        if is_healthy(result, p95_limit_ms, error_rate_limit)
    ]

    # Reports no ceiling when even the smallest measured run degraded.
    if not healthy_results:
        # Avoids inventing a positive capacity number.
        return None

    # Selects the greatest tested virtual-user level, not an unmeasured estimate.
    return max(
        healthy_results,
        key=lambda result: int(result["configured_peak_vus"]),
    )


# Runs one checked-in k6 scenario and loads its compact result artifact.
def run_k6(
    k6_binary: str,
    mode: str,
    peak_vus: int,
    arguments: argparse.Namespace,
    gateway_key: str,
) -> dict[str, Any]:
    # Creates the ignored local result directory before k6 writes into it.
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)

    # Gives every mode and concurrency point its own evidence file.
    result_path = RESULTS_DIRECTORY / f"load-{mode}-{peak_vus}.json"

    # Copies the current environment so gateway URL and model overrides still apply.
    process_environment = os.environ.copy()

    # Supplies secrets and scenario configuration through environment variables only.
    process_environment.update(
        {
            "GATEWAY_KEY": gateway_key,
            "PEAK_VUS": str(peak_vus),
            "STAGE_DURATION": arguments.stage_duration,
            "P95_LIMIT_MS": str(arguments.p95_limit_ms),
            "ERROR_RATE_LIMIT": str(arguments.error_rate_limit),
            "STREAM": "true" if mode == "streaming" else "false",
            "RESULT_FILE": str(result_path),
        }
    )

    # Runs the fixed script without shell interpolation or secret command arguments.
    completed = subprocess.run(
        [k6_binary, "run", str(K6_SCRIPT_PATH)],
        env=process_environment,
        check=False,
    )

    # Requires the summary artifact even when k6 exits nonzero for failed thresholds.
    if not result_path.exists():
        # Explains the failed run without exposing environment configuration.
        raise RuntimeError(
            f"k6 did not create a result for {mode} at {peak_vus} VUs "
            f"(exit code {completed.returncode})."
        )

    # Parses the compact checked-in schema emitted by handleSummary().
    result = json.loads(result_path.read_text(encoding="utf-8"))

    # Records k6's exit code separately from measured application error rate.
    result["k6_exit_code"] = completed.returncode

    # Returns this observed point to the combined sweep report.
    return result


# Executes selected modes at every level and reports only measured ceilings.
def run_sweep(arguments: argparse.Namespace) -> dict[str, object]:
    # Requires a real k6 installation before looking up gateway credentials.
    k6_binary = shutil.which("k6")
    if k6_binary is None:
        # Gives one actionable dependency message without attempting a substitute test.
        raise SystemExit("k6 is not installed; install it before running the load sweep.")

    # Accepts the k6-specific name or reuses the benchmark virtual-key variable.
    gateway_key = os.getenv("GATEWAY_KEY") or require_environment_value(
        "PROXY_VIRTUAL_KEY"
    )

    # Expands the convenient both option into two independent load sweeps.
    modes = (
        ["complete", "streaming"]
        if arguments.mode == "both"
        else [arguments.mode]
    )

    # Holds every measured point and final ceiling for each response mode.
    mode_reports: dict[str, object] = {}

    # Runs complete and streaming modes separately so one cannot hide the other.
    for mode in modes:
        # Executes each explicitly requested concurrency point in ascending order.
        results = [
            run_k6(
                k6_binary,
                mode,
                level,
                arguments,
                gateway_key,
            )
            for level in arguments.levels
        ]

        # Selects the highest tested point meeting both thresholds.
        ceiling = find_ceiling(
            results,
            arguments.p95_limit_ms,
            arguments.error_rate_limit,
        )

        # Stores raw evidence and the defensible observed ceiling together.
        mode_reports[mode] = {
            "runs": results,
            "highest_healthy_tested": ceiling,
        }

    # Returns one combined report without keys or request content.
    return {
        "benchmark": "gateway_load_sweep",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "levels": arguments.levels,
        "p95_limit_ms": arguments.p95_limit_ms,
        "error_rate_limit": arguments.error_rate_limit,
        "modes": mode_reports,
    }


# Loads settings, runs k6 sweeps, and prints the measured ceilings honestly.
def main() -> None:
    # Loads ignored local variables without replacing exported test configuration.
    load_dotenv()

    # Parses every requested concurrency and threshold setting.
    arguments = build_parser().parse_args()

    # Executes all external k6 runs and aggregates their evidence.
    report = run_sweep(arguments)

    # Prints one line per selected response mode.
    for mode, mode_report in report["modes"].items():
        # Reads the optional highest healthy measured result.
        ceiling = mode_report["highest_healthy_tested"]

        # Reports failure honestly when the first tested level already degraded.
        if ceiling is None:
            # Directs the next run toward a smaller starting level.
            print(f"{mode}: no tested level met both thresholds")

        # Reports only the highest level actually observed as healthy.
        else:
            # Includes concurrency, throughput, and p95 at that measured point.
            print(
                f"{mode}: {ceiling['configured_peak_vus']} VUs, "
                f"{ceiling['requests_per_second']:.2f} req/s, "
                f"p95 {ceiling['latency_ms']['p95']:.2f} ms"
            )

    # Saves the combined evidence only when the caller requested a path.
    write_json_report(report, arguments.output)


# Runs only with `python -m benchmarks.load_sweep`.
if __name__ == "__main__":
    # Starts the external k6 orchestration.
    main()
