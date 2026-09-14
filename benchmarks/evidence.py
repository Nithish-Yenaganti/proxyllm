"""Secret-free run provenance and streaming measurements."""
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]


def provenance():
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    status = git('status', '--porcelain')
    return {'measured_at': datetime.now(timezone.utc).isoformat(),
            'commit': git('rev-parse', 'HEAD'), 'dirty': None if status is None else bool(status),
            'python': platform.python_version(), 'platform': platform.system(),
            'scope': 'benchmark client checkout; remote gateway revision must be supplied separately'}


async def measure_request(client, url, headers, body):
    """TTFT starts at request submission and ends at first nonempty content delta.

    Does not retain prompts, headers, generated content, or exception text.
    """
    started = perf_counter()
    result = {'status': 'transport_error', 'ttft_ms': None, 'complete': False,
              'cache': None, 'estimated_cost_usd': None, 'avoided_cost_usd': None}
    try:
        async with client.stream('POST', url, headers=headers, json=body) as response:
            result['status'] = str(response.status_code)
            result['cache'] = response.headers.get('X-Proxy-Cache')
            result['estimated_cost_usd'] = response.headers.get('X-Proxy-Estimated-Cost-USD')
            result['avoided_cost_usd'] = response.headers.get('X-Proxy-Cost-Avoided-USD')
            if body.get('stream') and response.status_code == 200:
                provider_error = False
                done = False
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        done = True
                        continue
                    event = json.loads(data)
                    provider_error |= 'error' in event
                    if result['ttft_ms'] is None and any(
                        choice.get('delta', {}).get('content') for choice in event.get('choices', [])
                    ):
                        result['ttft_ms'] = (perf_counter()-started)*1000
                result['complete'] = done and not provider_error
            else:
                payload = await response.aread()
                if response.status_code == 200:
                    decoded = json.loads(payload)
                    result['complete'] = bool(decoded.get('choices')) and 'error' not in decoded
    except Exception as error:
        # Store type only: HTTP exception messages may contain private endpoint details.
        result['error_type'] = type(error).__name__
    result['total_ms'] = (perf_counter()-started)*1000
    return result
