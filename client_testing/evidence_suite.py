"""Run a bounded, free local benchmark matrix; never calls real providers."""
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.common import write_json_report
from client_testing.http_load import run


async def suite(output):
    output.mkdir(parents=True, exist_ok=False)
    cases = [(f'concurrency-{n}', dict(clients=n, interval=0.25)) for n in (1, 5, 10)]
    cases += [('streaming', dict(clients=5, interval=0.25, stream=True)),
              ('mixed-cache', dict(clients=2, interval=0.25, cache_every=2))]
    for name, options in cases:
        report = await run(seconds=3, pooled=True, **options)
        write_json_report(report, str(output / f'{name}.json'))
        print(f"{name}: {report['successful']}/{report['attempted']} complete; "
              f"{report['successful_requests_per_second']:.2f} successful requests/s; "
              f"statuses={report['statuses']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('benchmarks/results') /
                        datetime.now(timezone.utc).strftime('evidence-%Y%m%dT%H%M%S%fZ'))
    args = parser.parse_args()
    asyncio.run(suite(args.output))
    print(f'Saved fresh reports to {args.output}')


if __name__ == '__main__':
    main()
