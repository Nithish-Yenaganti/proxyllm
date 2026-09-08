"""Reuse the existing cache workload benchmark."""
import runpy
from client_testing.environment import load_client_environment

if __name__ == "__main__":
    load_client_environment()
    runpy.run_module("benchmarks.cache_workload", run_name="__main__")
