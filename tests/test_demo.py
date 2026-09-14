import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from client_testing.demo import run


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_checks_and_records_without_real_providers(self):
        cache = set()
        def respond(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'message': 'healthy'})
            token = request.headers['authorization']
            body = json.loads(request.content)
            if token == 'Bearer invalid-demo-key':
                return httpx.Response(401, json={})
            if token == 'Bearer second' and body['model'].startswith('anthropic/'):
                return httpx.Response(403, json={})
            if body.get('stream'):
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n')
            identity = (token, request.content)
            hit = identity in cache
            cache.add(identity)
            return httpx.Response(200, headers={'X-Proxy-Cache': 'HIT' if hit else 'MISS'},
                                  json={'choices': [{'message': {'content': 'hello'}, 'finish_reason': 'stop'}], 'usage': {}})
        factory = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'PROXY_VIRTUAL_KEY': 'first', 'PROXY_SECOND_VIRTUAL_KEY': 'second'}), \
             patch('client_testing.demo.load_client_environment'), \
             patch('client_testing.demo.asyncio.sleep', new_callable=AsyncMock), \
             patch('client_testing.demo.httpx.AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(respond), **kw)):
            output = Path(directory) / 'demo.json'
            await run('http://localhost', output)
            result = json.loads(output.read_text())
            self.assertTrue(result['passed'])
            self.assertEqual(len(result['checks']), 10)
            self.assertNotIn('Bearer', output.read_text())
