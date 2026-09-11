"""Run the real gateway routes in-process with an isolated mock provider."""
import asyncio
import json
import tempfile
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from api import main
from api.middleware import VirtualKeyAuthMiddleware
from auth import database
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key
from providers.anthropic import AnthropicAdapter


async def run():
    captured = []

    def provider(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_demo", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5", "content": [{"type": "text", "text": "Hello from the mock provider"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 8, "output_tokens": 6},
        })

    with tempfile.TemporaryDirectory(prefix='proxy-inspection-demo-') as directory:
        db = Path(directory) / 'demo.db'
        await database.initialize_database(db)
        key = generate_virtual_key()
        await database.create_virtual_key_record('demo', get_key_prefix(key), hash_virtual_key(key), 'anthropic', 'default', db)
        app = FastAPI()
        app.add_middleware(VirtualKeyAuthMiddleware, database_path=db)
        app.post('/v1/inspect')(main.chat_completions)
        app.post('/v1/chat/completions')(main.chat_completions)
        app.state.provider_adapters = {'anthropic': AnthropicAdapter(
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)))}
        credentials = {('anthropic', 'default'): {'url': 'https://mock.invalid/messages', 'api_key': 'mock-only'}}
        with patch.object(main, 'PROVIDER_CREDENTIALS', credentials), \
             patch.object(main, 'get_provider_permission_for_key', partial(database.get_provider_permission_for_key, database_path=db)), \
             patch.object(main, 'create_usage_log_record', partial(database.create_usage_log_record, database_path=db)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway', headers={'Authorization': f'Bearer {key}'}) as client:
                body = {'model': 'anthropic/claude-sonnet-5', 'messages': [
                    {'role': 'system', 'content': 'Be helpful.'},
                    {'role': 'user', 'content': 'Hello'}], 'max_tokens': 50, 'cache': False}
                blocked = await client.post('/v1/inspect', json={**body, 'temperature': 0.2})
                assert blocked.status_code == 400
                print('1. Unsupported request:', json.dumps(blocked.json()))
                preview = await client.post('/v1/inspect', json=body)
                assert preview.status_code == 200
                assert captured == [], 'Inspection contacted the provider'
                print('2. Prepared request:', json.dumps(preview.json(), indent=2))
                response = await client.post('/v1/chat/completions', json=body)
                assert response.status_code == 200, response.text
                assert captured == [preview.json()['payload']], 'Preview differs from outbound HTTP body'
                print('3. Execution:', response.json()['choices'][0]['message']['content'])
                print('PASS: zero provider calls during inspection; one mock call during execution; payloads match.')


if __name__ == '__main__':
    asyncio.run(run())
