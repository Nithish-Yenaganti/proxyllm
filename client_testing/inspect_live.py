"""Test inspection and two small paid provider calls using saved virtual keys."""
import json
import sqlite3
from pathlib import Path

import httpx
from dotenv import dotenv_values


def counts():
    with sqlite3.connect(Path(__file__).resolve().parents[1] / 'auth/gateway.db') as db:
        return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('usage_logs', 'response_cache')}


def run():
    settings = dotenv_values(Path(__file__).with_name('.env'))
    key_a = settings.get('PROXY_VIRTUAL_KEY')
    key_b = settings.get('PROXY_SECOND_VIRTUAL_KEY')
    if not key_a or not key_b:
        raise SystemExit('Set both existing virtual keys in client_testing/.env.')
    body = {'model': 'anthropic/claude-sonnet-5', 'messages': [
        {'role': 'system', 'content': 'Answer briefly.'},
        {'role': 'user', 'content': 'Reply with just HELLO.'}],
        'max_tokens': 50, 'cache': False}
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=90) as client:
        def post(path, key, payload):
            return client.post(path, headers={'Authorization': f'Bearer {key}'}, json=payload)
        before = counts()
        denied = post('/v1/inspect', key_b, body)
        assert denied.status_code == 403, f'Permission check: HTTP {denied.status_code}'
        print('PASS client-b: Anthropic inspection denied (403).')
        blocked = post('/v1/inspect', key_a, {**body, 'temperature': 0.2})
        assert blocked.status_code == 400 and blocked.json().get('status') == 'blocked'
        print('PASS client-a: unsupported temperature blocked (400).')
        prepared = post('/v1/inspect', key_a, body)
        assert prepared.status_code == 200 and prepared.json().get('status') == 'prepared'
        payload = prepared.json()['payload']
        assert payload['system'] == 'Answer briefly.' and 'cache' not in payload
        print('PASS client-a: Anthropic prepared (200); system translated, cache removed.')
        fireworks = {**body, 'model': 'fireworks/deepseek-v4-flash'}
        prepared_b = post('/v1/inspect', key_b, fireworks)
        assert prepared_b.status_code == 200 and prepared_b.json().get('status') == 'prepared'
        print('PASS client-b: Fireworks prepared (200).')
        assert counts() == before, 'Inspection changed usage/cache row counts (check concurrent traffic).'
        print('PASS inspection left usage and cache row counts unchanged.')
        failures = []
        for name, key, request in [('Anthropic', key_a, body), ('Fireworks', key_b, fireworks)]:
            response = post('/v1/chat/completions', key, request)
            if response.status_code != 200:
                print(f'FAIL {name} live call: HTTP {response.status_code} (error body withheld to protect credentials).')
                failures.append(name)
                continue
            result = response.json()
            answer = result['choices'][0]['message'].get('content')
            print(f'PASS {name} live call: HTTP 200; answer={json.dumps(answer)}; usage={json.dumps(result.get("usage"))}')
        print('Usage/cache row counts:', json.dumps({'before': before, 'after': counts()}))
        if failures:
            raise SystemExit('Live provider failures: ' + ', '.join(failures))


if __name__ == '__main__':
    run()
