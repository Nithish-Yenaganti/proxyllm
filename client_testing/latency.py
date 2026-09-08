"""Reuse the existing direct-versus-proxy latency benchmark."""
import runpy
from client_testing.environment import load_client_environment

if __name__ == "__main__":
    load_client_environment()
    runpy.run_module("benchmarks.latency_overhead", run_name="__main__")
