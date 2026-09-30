"""Small real-provider demo against an already-running local gateway (paid calls)."""
import argparse
import asyncio
import os
import uuid
from pathlib import Path

import httpx
from benchmarks.common import write_json_report
from benchmarks.evidence import provenance, measure_request
from client_testing.environment import load_client_environment


async def run(base_url, output):
    load_client_environment()
    key = os.environ.get('PROXY_VIRTUAL_KEY')
    second = os.environ.get('PROXY_SECOND_VIRTUAL_KEY')
    if not key or not second or key == second:
        raise ValueError('Configure two distinct virtual keys first.')
    report = {'provenance': provenance(), 'checks': [], 'passed': False,
              'scope': 'Local real-provider demo; not capacity or answer-quality evaluation.',
              'max_output_tokens': 256, 'automatic_retries': False}
    async with httpx.AsyncClient(base_url=base_url, timeout=60, trust_env=False) as client:
        async def check(name, body, token=key, expected=200, cache=None, stream=False):
            await asyncio.sleep(3)
            headers = {'Authorization': 'Bearer ' + token}
            if stream:
                sample = await measure_request(client, base_url + '/v1/chat/completions', headers, body)
                passed = sample['status'] == '200' and sample['complete'] and sample['ttft_ms'] is not None
            else:
                response = await client.post('/v1/chat/completions', headers=headers, json=body)
                sample = {'status': response.status_code, 'cache': response.headers.get('X-Proxy-Cache')}
                passed = response.status_code == expected
                if expected == 200 and passed:
                    data = response.json()
                    choices = data.get('choices', [])
                    passed = bool(choices and choices[0].get('message', {}).get('content'))
                    sample['finish_reason'] = choices[0].get('finish_reason') if choices else None
                    passed = passed and sample['finish_reason'] != 'length'
                    sample['usage'] = data.get('usage', {})
                if cache is not None:
                    passed = passed and sample['cache'] == cache
            report['checks'].append({'name': name, 'passed': passed, **sample})
            write_json_report(report, str(output))
            print(f'{name}: {"PASS" if passed else "FAIL"}', flush=True)
            if not passed:
                raise RuntimeError('Demo stopped at ' + name + '; see saved report.')

        try:
            health = await client.get('/')
            if health.status_code != 200:
                raise RuntimeError('Gateway root health check failed.')
            report['health_passed'] = True
            # No provider call for malformed input even if an unexpected key is valid.
            await check('invalid-key-denied', {}, token='invalid-demo-key', expected=401)
            anthropic = {'model': 'anthropic/claude-sonnet-5', 'max_tokens': 256,
                         'messages': [{'role': 'user', 'content': 'Reply with hello.'}], 'cache': False}
            await check('client-b-anthropic-denied', anthropic, token=second, expected=403)
            for provider, model in [('fireworks', 'fireworks/deepseek-v4-flash'),
                                    ('anthropic', 'anthropic/claude-sonnet-5')]:
                body = {'model': model, 'max_tokens': 256, 'cache': True,
                        'messages': [{'role': 'user', 'content': 'Reply with exactly hello. Demo ' + uuid.uuid4().hex}]}
                await check(provider + '-chat-cache-miss', body, cache='MISS')
                await check(provider + '-cache-hit', body, cache='HIT')
                if provider == 'fireworks':
                    await check('client-b-isolated-cache-miss', body, token=second, cache='MISS')
                    await check('client-b-cache-hit', body, token=second, cache='HIT')
                await check(provider + '-stream', {**body, 'cache': False, 'stream': True}, stream=True)
            report['passed'] = True
        except Exception as error:
            report['error_type'] = type(error).__name__
            raise
        finally:
            write_json_report(report, str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, default=Path('benchmarks/results') / ('demo-' + uuid.uuid4().hex + '.json'))
    parser.add_argument('--allow-paid', action='store_true')
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error('Explicit --allow-paid is required; this contacts real providers.')
    if args.output.exists():
        parser.error('Choose a new report path; existing results are not overwritten.')
    url = httpx.URL(args.base_url)
    if url.host not in ('localhost', '127.0.0.1') or url.scheme != 'http' or url.path not in ('', '/'):
        parser.error('Use a local gateway origin, without a path.')
    asyncio.run(run(args.base_url.rstrip('/'), args.output))


if __name__ == '__main__':
    main()
