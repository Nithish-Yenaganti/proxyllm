"""Share percentile, environment, and JSON report helpers across benchmarks."""

# Encodes benchmark reports in a portable human-readable format.
import json

# Reads secrets by variable name without printing their values.
import os

# Resolves optional report output paths and creates their parent directories.
from pathlib import Path

# Supplies flexible report and metric type annotations.
from typing import Any


# Returns one required environment value or stops with a safe explanation.
def require_environment_value(name: str) -> str:
    # Reads the requested secret or configuration from process memory.
    value = os.getenv(name)

    # Rejects missing and empty values without displaying sensitive content.
    if value is None or value == "":
        # Names only the configuration variable the user must provide.
        raise SystemExit(f"Set {name} before running this benchmark.")

    # Returns the non-empty value only to the local benchmark caller.
    return value


# Calculates a linearly interpolated percentile for one non-empty sample.
def percentile(values: list[float], percentile_value: float) -> float:
    # Requires the mathematical percentile interval accepted by this helper.
    if not 0 <= percentile_value <= 100:
        # Prevents silently incorrect benchmark reports.
        raise ValueError("percentile must be between 0 and 100")

    # Requires at least one completed request measurement.
    if not values:
        # Gives benchmark callers a clear failure instead of dividing by zero.
        raise ValueError("at least one value is required")

    # Sorts a copy so the original request order remains available for raw reports.
    ordered = sorted(values)

    # Maps the requested percentile onto the zero-based sample positions.
    position = (len(ordered) - 1) * percentile_value / 100

    # Finds the observed values immediately below and above that position.
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)

    # Calculates the fractional distance between the two observed values.
    weight = position - lower_index

    # Interpolates smoothly while returning exact observed endpoints.
    return (
        ordered[lower_index] * (1 - weight)
        + ordered[upper_index] * weight
    )


# Summarizes request duration samples using interview-relevant tail percentiles.
def summarize_latencies(values_ms: list[float]) -> dict[str, float]:
    # Returns consistently rounded measurements for console and JSON comparison.
    return {
        "p50_ms": round(percentile(values_ms, 50), 3),
        "p95_ms": round(percentile(values_ms, 95), 3),
        "p99_ms": round(percentile(values_ms, 99), 3),
    }


# Writes one optional JSON artifact while always keeping console reporting possible.
def write_json_report(report: dict[str, Any], output_path: str | None) -> None:
    # Leaves the filesystem unchanged when the caller requested console output only.
    if output_path is None:
        # Makes report files opt in rather than silently accumulating artifacts.
        return

    # Resolves the user-selected path without assuming a current directory layout.
    destination = Path(output_path)

    # Creates only the report's specific parent directory when necessary.
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Writes stable formatted JSON suitable for comparing later architecture changes.
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Confirms the artifact location without displaying any key or request headers.
    print(f"Saved report to {destination}")
