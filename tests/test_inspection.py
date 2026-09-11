"""Exercise inspection through authenticated HTTP and real adapter transports."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from api import main
from api.middleware import VirtualKeyAuthMiddleware
from auth.database import initialize_database, create_virtual_key_record, revoke_virtual_key_record
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key
from providers.anthropic import AnthropicAdapter
from providers.fireworks import FireworksAdapter
from providers.base import AdapterRequest, ProviderCredential, ProviderRequestError
from providers.inspection import inspect_payload


async def connected():
    return False


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_matches_wire_for_both_providers_and_modes(self):
        for adapter_type in (AnthropicAdapter, FireworksAdapter):
            for stream in (False, True):
                with self.subTest(provider=adapter_type.name, stream=stream):
                    captured = []
                    def transport(req):
                        captured.append(json.loads(req.content))
                        # A provider rejection still captures the actual outbound body,
                        # and exercises stream cleanup without fabricated SSE events.
                        return httpx.Response(400, json={"error": {"message": "fixture"}})
                    adapter = adapter_type(lambda: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
                    body = {"model": "alias", "messages": [
                        {"role": "system", "content": "Be helpful"},
                        {"role": "user", "content": "Hi"}],
                        "max_tokens": 20, "stream": stream, "stop": ["END"]}
                    original = deepcopy(body)
                    req = AdapterRequest(body, "alias", "real-model", ProviderCredential("https://mock.test/messages", "secret"), connected)
                    report = inspect_payload(adapter, req, original_body=body)
                    self.assertEqual(captured, [])
                    self.assertEqual(report['status'], 'prepared')
                    await adapter.send(req)
                    self.assertEqual(captured, [report['payload']])
                    self.assertEqual(body, original)
                    self.assertNotIn('secret', json.dumps(report))
                    if adapter.name == 'anthropic':
                        self.assertEqual(report['payload']['system'], 'Be helpful')
                    elif stream:
                        self.assertTrue(report['payload']['stream_options']['include_usage'])

    async def test_blocked_error_matches_send_without_connecting(self):
        def forbidden():
            self.fail('Preparation must not create an HTTP client')
        adapter = AnthropicAdapter(forbidden)
        req = AdapterRequest({'model': 'alias', 'messages': [{'role':'user','content':'Hi'}], 'temperature': 0.2}, 'alias', 'upstream', ProviderCredential('https://mock.test', 'secret'), connected)
        report = inspect_payload(adapter, req, original_body=req.body)
        with self.assertRaises(ProviderRequestError) as error:
            await adapter.send(req)
        self.assertEqual(report['errors'][0]['message'], str(error.exception))
        self.assertIsNone(report['payload'])
        self.assertEqual(report['status'], 'blocked')


class InspectionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(main, 'PARAMETER_DROP_POLICY', {}))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'test.db'
        await initialize_database(self.db)
        self.key = generate_virtual_key()
        self.key_id = await create_virtual_key_record('test', get_key_prefix(self.key), hash_virtual_key(self.key), 'anthropic', 'default', self.db)
        self.app = FastAPI()
        self.app.add_middleware(VirtualKeyAuthMiddleware, database_path=self.db)
        self.app.post('/v1/inspect')(main.chat_completions)
        self.app.post('/v1/chat/completions')(main.chat_completions)
        def forbidden():
            self.fail('Inspection must not create a provider connection')
        self.app.state.provider_adapters = {'anthropic': AnthropicAdapter(forbidden), 'fireworks': FireworksAdapter(forbidden)}
        from auth.database import get_provider_permission_for_key
        async def permission(key_id, provider):
            return await get_provider_permission_for_key(key_id, provider, self.db)
        self.enterContext(patch.object(main, 'get_provider_permission_for_key', side_effect=permission))
        self.enterContext(patch.object(main, 'PROVIDER_CREDENTIALS', {('anthropic', 'default'): {'url':'https://private.test', 'api_key':'secret-provider-key'}}))
        self.usage = self.enterContext(patch.object(main, 'record_request_usage', new_callable=AsyncMock))
        self.cache = self.enterContext(patch.object(main, 'read_cached_response', new_callable=AsyncMock))
        self.cache_write = self.enterContext(patch.object(main, 'write_cached_response', new_callable=AsyncMock))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test', headers={'Authorization': f'Bearer {self.key}'})
        self.addAsyncCleanup(self.client.aclose)
        self.body = {'model':'anthropic/claude-sonnet-5', 'messages':[{'role':'user','content':'Hi'}], 'cache':True}

    async def asyncTearDown(self):
        self.usage.assert_not_awaited()
        self.cache.assert_not_awaited()
        self.cache_write.assert_not_awaited()

    async def test_prepared_no_secrets_no_side_effects(self):
        response = await self.client.post('/v1/inspect', json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(data['status'], 'prepared')
        self.assertFalse(data['upstream_verified'])
        self.assertNotIn('cache', data['payload'])
        self.assertIn({'field':'cache','action':'removed'}, data['changes'])
        for secret in ('secret-provider-key', 'private.test', self.key):
            self.assertNotIn(secret, response.text)

    async def test_blocked(self):
        response = await self.client.post('/v1/inspect', json={**self.body, 'temperature':0.2})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'blocked')
        self.assertIn('temperature', response.text)

    async def test_auth_permission_and_revocation(self):
        response = await self.client.post('/v1/inspect', json=self.body, headers={'Authorization':''})
        self.assertEqual(response.status_code, 401)
        response = await self.client.post('/v1/inspect', json={**self.body,'model':'fireworks/deepseek-v4-flash'})
        self.assertEqual(response.status_code, 403)
        await revoke_virtual_key_record(self.key_id, self.db)
        response = await self.client.post('/v1/inspect', json=self.body)
        self.assertEqual(response.status_code, 401)

    async def test_invalid_body_unknown_model_and_size(self):
        for body, expected in [('not json',400), ('[]',400), ('{}',400), ('{"model":"missing"}',404)]:
            response = await self.client.post('/v1/inspect', content=body)
            self.assertEqual(response.status_code, expected)
        with patch.object(main, 'MAX_REQUEST_BYTES', 4):
            response = await self.client.post('/v1/inspect', content=b'12345')
            self.assertEqual(response.status_code, 413)

    async def test_rate_limit_shared_with_chat(self):
        for _ in range(24):
            response = await self.client.post('/v1/inspect', json=self.body)
            self.assertEqual(response.status_code, 200)
        for route in ('/v1/inspect', '/v1/chat/completions'):
            response = await self.client.post(route, json=self.body)
            self.assertEqual(response.status_code, 429)
            self.assertIn('retry-after', response.headers)
