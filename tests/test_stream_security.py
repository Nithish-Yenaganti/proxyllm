"""Exercise stream limits with isolated transports and stalled ASGI clients."""
import asyncio
import gzip
import zlib
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock

import httpx
from starlette.requests import ClientDisconnect

from providers.anthropic import AnthropicAdapter
from providers.base import AdapterRequest, ProviderCredential, ProviderConnectionError, ProviderRequestError
from providers.concurrency import GatewayBusy, ProviderSlots, SlotStreamingResponse
from providers.fireworks import FireworksAdapter
from providers.response_limits import BoundedDecoder, MAX_SSE_LINE_BYTES
from tests.test_streaming import TrackedByteStream
from usage.tracking import OpenAIStreamObserver, observe_stream


def request(stream=True, role="user"):
    return AdapterRequest(
        body={"model": "test", "messages": [{"role": role, "content": "Hello"}], "stream": stream},
        public_model="test", upstream_model="test",
        credential=ProviderCredential("https://provider.invalid/chat", "fixture"),
        is_disconnected=AsyncMock(return_value=False),
    )


class StreamLimitTests(IsolatedAsyncioTestCase):
    async def make_response(self, slots, *, body=None, total=.2, write=.02):
        upstream = TrackedByteStream([b'data: {"choices": []}\n\n', b'data: [DONE]\n\n'])
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=upstream)))
        response = await FireworksAdapter(lambda: client).send(request())
        finished = AsyncMock()
        observed = observe_stream(body or response.body, OpenAIStreamObserver(), finished)
        wrapped = SlotStreamingResponse(observed, release=await slots.acquire(),
            close=response.aclose, total_seconds=total, write_seconds=write)
        return wrapped, upstream, client, finished

    async def test_five_blocked_clients_release_all_slots_and_close_streams(self):
        slots = ProviderSlots(wait_seconds=.005)
        responses = [await self.make_response(slots, write=.06) for _ in range(5)]
        started = [asyncio.Event() for _ in range(5)]
        async def send(index, message):
            if message['type'] == 'http.response.body':
                started[index].set()
                await asyncio.Event().wait()
        tasks = [asyncio.create_task(item[0](
            {'type': 'http', 'asgi': {'spec_version': '2.4'}}, AsyncMock(),
            lambda message, index=index: send(index, message)))
            for index, item in enumerate(responses)]
        await asyncio.gather(*(event.wait() for event in started))
        with self.assertRaises(GatewayBusy):
            await slots.acquire()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, (TimeoutError, ClientDisconnect)) for result in results))
        self.assertEqual(slots.semaphore._value, 5)
        for _, upstream, client, finished in responses:
            self.assertTrue(upstream.was_closed)
            self.assertTrue(client.is_closed)
            finished.assert_awaited_once()
            self.assertTrue(finished.call_args.args[1])
            self.assertFalse(finished.call_args.args[0].completed)

    async def test_stalled_headers_close_unstarted_upstream(self):
        slots = ProviderSlots(limit=1)
        response, upstream, client, finished = await self.make_response(slots)
        async def send(message):
            await asyncio.Event().wait()
        with self.assertRaises((TimeoutError, ClientDisconnect)):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, AsyncMock(), send)
        self.assertTrue(upstream.was_closed)
        self.assertTrue(client.is_closed)
        self.assertEqual(slots.semaphore._value, 1)
        finished.assert_awaited_once()
        self.assertTrue(finished.call_args.args[1])

    async def test_completed_stream_logs_success_once(self):
        slots = ProviderSlots(limit=1)
        response, upstream, client, finished = await self.make_response(slots)
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, AsyncMock(), AsyncMock())
        self.assertTrue(upstream.was_closed)
        self.assertTrue(client.is_closed)
        finished.assert_awaited_once()
        self.assertFalse(finished.call_args.args[1])
        self.assertTrue(finished.call_args.args[0].completed)
        self.assertEqual(slots.semaphore._value, 1)

    async def test_total_deadline_stops_upstream_that_keeps_making_progress(self):
        closed = asyncio.Event()
        async def body():
            try:
                while True:
                    await asyncio.sleep(.002)
                    yield b'data: {}\n\n'
            finally:
                closed.set()
        slots = ProviderSlots(limit=1)
        response, _, _, finished = await self.make_response(slots, body=body(), total=.03, write=1)
        with self.assertRaises((TimeoutError, ClientDisconnect)):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, AsyncMock(), AsyncMock())
        self.assertTrue(closed.is_set())
        finished.assert_awaited_once()
        self.assertTrue(finished.call_args.args[1])
        self.assertEqual(slots.semaphore._value, 1)

    async def test_cancel_during_downstream_send_closes_suspended_iterator(self):
        slots = ProviderSlots(limit=1)
        response, upstream, client, finished = await self.make_response(slots, write=1)
        started = asyncio.Event()
        async def send(message):
            if message['type'] == 'http.response.body':
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, AsyncMock(), send))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(upstream.was_closed)
        self.assertTrue(client.is_closed)
        finished.assert_awaited_once()
        self.assertTrue(finished.call_args.args[1])
        self.assertEqual(slots.semaphore._value, 1)

    async def test_observation_limit_does_not_change_fireworks_bytes(self):
        chunks = [gzip.compress(b'x' * 2_000_000)]
        upstream = TrackedByteStream(chunks)
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=upstream)))
        response = await FireworksAdapter(lambda: client).send(request())
        observer = OpenAIStreamObserver('gzip')
        finished = AsyncMock()
        actual = [chunk async for chunk in observe_stream(response.body, observer, finished)]
        self.assertEqual(actual, chunks)
        self.assertEqual(response.headers['Content-Encoding'], 'gzip')
        self.assertTrue(observer.disabled)
        self.assertEqual(observer.buffer, b'')
        self.assertTrue(upstream.was_closed)
        self.assertTrue(client.is_closed)

    async def test_anthropic_rejects_oversized_compressed_line_and_closes(self):
        upstream = TrackedByteStream([gzip.compress(b'data: '+b'x' * 2_000_000)])
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=upstream)))
        response = await AnthropicAdapter(lambda: client).send(request())
        with self.assertRaises(ProviderConnectionError):
            _ = [chunk async for chunk in response.body]
        self.assertTrue(upstream.was_closed)
        self.assertTrue(client.is_closed)

    async def test_complete_and_error_bodies_enforce_decoded_limit_for_both_providers(self):
        for adapter in (AnthropicAdapter, FireworksAdapter):
            for stream, status in ((False, 200), (True, 500)):
                with self.subTest(adapter=adapter.__name__, stream=stream):
                    upstream = TrackedByteStream([gzip.compress(b'x' * 5_000_001)])
                    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
                        httpx.Response(status, headers={'content-encoding': 'gzip'}, stream=upstream)))
                    with self.assertRaises(ProviderConnectionError):
                        await adapter(lambda: client).send(request(stream))
                    self.assertTrue(upstream.was_closed)
                    self.assertTrue(client.is_closed)

    async def test_adapters_request_only_supported_compression(self):
        for adapter in (AnthropicAdapter, FireworksAdapter):
            for streaming in (False, True):
                with self.subTest(adapter=adapter.__name__, streaming=streaming):
                    captured = []
                    def respond(req):
                        captured.append(req.headers["accept-encoding"])
                        return httpx.Response(200, json={})
                    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                    response = await adapter(lambda: client).send(request(streaming))
                    self.assertEqual(captured, ["gzip, deflate"])
                    if response.aclose is not None:
                        await response.aclose()

    async def test_anthropic_invalid_roles_reject_before_connecting(self):
        for role in ([], {}, "x" * 100000):
            factory = Mock(side_effect=AssertionError('No client should be created'))
            adapter = AnthropicAdapter(factory)
            with self.assertRaises(ProviderRequestError) as preparation:
                adapter.prepare(request(role=role))
            with self.assertRaises(ProviderRequestError) as execution:
                await adapter.send(request(role=role))
            self.assertEqual(str(preparation.exception), str(execution.exception))
            self.assertLess(len(str(execution.exception)), 150)
            factory.assert_not_called()


class ObservationLimitTests(TestCase):
    def test_gzip_and_deflate_bombs_disable_observation(self):
        for encoding, compress in (('gzip', gzip.compress), ('deflate', zlib.compress)):
            observer = OpenAIStreamObserver(encoding)
            observer.feed(compress(b'x' * 2_000_000))
            self.assertTrue(observer.disabled)
            self.assertEqual(observer.buffer, b'')
            observer.feed(b'data: [DONE]\n\n')
            self.assertFalse(observer.observation.completed)

    def test_unfinished_line_cannot_grow_across_chunks(self):
        observer = OpenAIStreamObserver()
        observer.feed(b'x' * MAX_SSE_LINE_BYTES)
        self.assertEqual(len(observer.buffer), MAX_SSE_LINE_BYTES)
        observer.feed(b'x')
        self.assertTrue(observer.disabled)
        self.assertEqual(observer.buffer, b'')

    def test_many_small_events_have_total_decode_budget(self):
        decoder = BoundedDecoder(limit=32)
        self.assertEqual(b''.join(decoder.feed(b'x' * 16)), b'x' * 16)
        with self.assertRaises(ProviderConnectionError):
            list(decoder.feed(b'x' * 17))

    def test_normal_usage_handles_arbitrary_compression_and_line_boundaries(self):
        payload = b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3}}\r\ndata: [DONE]\r'
        for encoding, encoded in ((None, payload), ('gzip', gzip.compress(payload)), ('deflate', zlib.compress(payload))):
            observer = OpenAIStreamObserver(encoding)
            for byte in encoded:
                observer.feed(bytes([byte]))
            self.assertFalse(observer.disabled)
            self.assertTrue(observer.observation.completed)
            self.assertEqual(observer.observation.usage.total_tokens, 5)

    def test_malformed_json_and_compression_do_not_escape_observer(self):
        observer = OpenAIStreamObserver()
        observer.feed(b'data: ' + b'[' * 2000 + b']' * 2000 + b'\n')
        observer.feed(b'data: [DONE]\n')
        self.assertTrue(observer.observation.completed)
        observer = OpenAIStreamObserver('gzip')
        observer.feed(b'not gzip')
        self.assertTrue(observer.disabled)
