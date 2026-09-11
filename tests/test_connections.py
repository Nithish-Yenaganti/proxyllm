import unittest
import httpx
from providers.connections import BorrowedClient, pooled_adapters, NoCookies
from providers.anthropic import validate_anthropic_settings
from providers.base import ProviderRequestError
from providers.base import AdapterRequest, ProviderCredential, forward_raw_stream
from unittest.mock import AsyncMock


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_settings_rejected_before_cache_lookup(self):
        from api import main
        from tests.test_routing import build_chat_request, RecordingAdapter
        from unittest.mock import patch
        cache = AsyncMock(return_value={'cached': True})
        writer = AsyncMock()
        adapter = RecordingAdapter('anthropic')
        with patch.object(main, 'get_provider_permission_for_key', AsyncMock(return_value={'provider_credential':'default'})), \
             patch.object(main, 'PARAMETER_DROP_POLICY', {}), \
             patch.dict(main.PROVIDER_CREDENTIALS, {('anthropic','default'):{'api_key':'fake','url':'https://example.test'}}), \
             patch.object(main, 'get_provider_adapter', return_value=adapter), \
             patch.object(main, 'read_cached_response', cache), \
             patch.object(main, 'create_usage_log_record', writer):
            response = await main.chat_completions(build_chat_request('anthropic/claude-sonnet-5',
                extra_body={'cache':True, 'temperature':0}))
        self.assertEqual(response.status_code,400)
        cache.assert_not_awaited()
        writer.assert_awaited_once()
        self.assertIsNone(adapter.received_request)

    async def test_stream_disconnect_closes_response_not_pool(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'first'
                yield b'second'
        response=httpx.Response(200, stream=Body())
        async with httpx.AsyncClient() as pool:
            request=AdapterRequest({}, 'test', 'test', ProviderCredential('https://example.test','fake'), AsyncMock(return_value=True))
            self.assertEqual([c async for c in forward_raw_stream(request,response,BorrowedClient(pool))], [])
            self.assertTrue(response.is_closed)
            self.assertFalse(pool.is_closed)

    async def test_stream_cleanup_keeps_borrowed_pool_open(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: [DONE]\n\n'
        response=httpx.Response(200, stream=Body())
        async with httpx.AsyncClient() as pool:
            request=AdapterRequest({}, 'test', 'test', ProviderCredential('https://example.test','fake'), AsyncMock(return_value=False))
            chunks=[chunk async for chunk in forward_raw_stream(request,response,BorrowedClient(pool))]
            self.assertEqual(chunks,[b'data: [DONE]\n\n'])
            self.assertTrue(response.is_closed)
            self.assertFalse(pool.is_closed)

    async def test_borrowed_handle_does_not_close_pool(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True}))) as pool:
            async with BorrowedClient(pool) as first:
                await first.post('https://example.test')
            self.assertFalse(pool.is_closed)
            await BorrowedClient(pool).post('https://example.test')
        self.assertTrue(pool.is_closed)

    async def test_pool_lifecycle_and_provider_separation(self):
        async with pooled_adapters() as adapters:
            a=adapters['fireworks'].client_factory()
            b=adapters['fireworks'].client_factory()
            c=adapters['anthropic'].client_factory()
            self.assertIs(a.client,b.client)
            self.assertIsNot(a.client,c.client)
            await a.aclose()
            self.assertFalse(b.client.is_closed)
        self.assertTrue(b.client.is_closed)
        self.assertTrue(c.client.is_closed)

    async def test_provider_cookies_are_not_retained(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, headers={'set-cookie':'session=secret'}))) as client:
            client._cookies=NoCookies()
            await client.get('https://example.test')
            self.assertEqual(len(client.cookies),0)


class SettingsTests(unittest.TestCase):
    def test_settings_are_explicit(self):
        for extra in ({'temperature':0}, {'top_p':1}, {'stream':'yes'},
                      {'max_tokens':10,'max_completion_tokens':20}, {'stream_options':{'include_usage':False}}):
            with self.subTest(extra=extra), self.assertRaises(ProviderRequestError):
                validate_anthropic_settings({'model':'test','messages':[],**extra})
        validate_anthropic_settings({'model':'test','messages':[], 'stream':True,
                                    'stream_options':{'include_usage':True}})
