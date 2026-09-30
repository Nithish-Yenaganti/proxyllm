"""Untrusted requests must fail before provider work or large usage writes."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from fastapi import HTTPException
from api import main
from api.request_body import RequestBodyTimeout, configured_body_timeout, read_json_body
from dashboard import management
from tests.test_request_body import request_for


class RequestSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_rejections_do_not_echo_or_log_input(self):
        for model, expected in [('x' * 1000000, 400), ('unknown/private-name', 404)]:
            for path in ['/v1/inspect', '/v1/chat/completions']:
                request, _ = request_for([json.dumps({'model': model}).encode()])
                request.scope['path'] = path
                with patch.object(main, 'record_request_usage', new_callable=AsyncMock) as usage, \
                     patch.object(main, 'get_provider_permission_for_key', new_callable=AsyncMock) as permissions:
                    response = await main.chat_completions(request)
                self.assertEqual(response.status_code, expected)
                self.assertLess(len(response.body), 400)
                self.assertNotIn(model.encode(), response.body)
                permissions.assert_not_awaited()
                if path.endswith('inspect'):
                    usage.assert_not_awaited()
                else:
                    usage.assert_awaited_once()
                    self.assertIsNone(usage.await_args.args[2])

    async def test_invalid_json_values_return_400_before_routing(self):
        bodies = [b'{"n":' + b'1' * 5000 + b'}', b'{"n":NaN}',
                  b'{"n":Infinity}', b'{"n":-Infinity}', b'{"n":1e999}',
                  b'{"s":"\\ud800"}', b'{"\\udfff":0}', b'{"s":"\xff"}',
                  b'[' * 1100 + b'0' + b']' * 1100]
        for path in ['/v1/inspect', '/v1/chat/completions']:
            for body in bodies:
                with self.subTest(path=path, body_prefix=body[:30]):
                    request, _ = request_for([body])
                    request.scope['path'] = path
                    with patch.object(main, 'record_request_usage', new_callable=AsyncMock), \
                         patch.object(main, 'get_model_route') as route:
                        response = await main.chat_completions(request)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(json.loads(response.body)['error']['code'], 'invalid_json')
                    route.assert_not_called()

    async def test_valid_unicode_and_numbers_remain_supported(self):
        value = {'message': 'Hello 👋', 'number': 1.25, 'tokens': 2048}
        request, _ = request_for([json.dumps(value).encode()])
        self.assertEqual(await read_json_body(request, 1000), value)

    async def test_deadline_cancels_stalled_receive(self):
        cancelled = asyncio.Event()
        async def receive():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        request = Request({'type': 'http', 'headers': []}, receive)
        with self.assertRaises(RequestBodyTimeout):
            await read_json_body(request, 1000, timeout=0.01)
        self.assertTrue(cancelled.is_set())

    async def test_total_deadline_is_not_reset_by_chunks(self):
        async def receive():
            await asyncio.sleep(0.01)
            return {'type': 'http.request', 'body': b' ', 'more_body': True}
        request = Request({'type': 'http', 'headers': []}, receive)
        with self.assertRaises(RequestBodyTimeout):
            await asyncio.wait_for(read_json_body(request, 1000, timeout=0.035), 0.5)

    async def test_timeout_responses_do_not_route(self):
        for path in ['/v1/inspect', '/v1/chat/completions']:
            request, _ = request_for([b'{}'])
            request.scope['path'] = path
            with patch.object(main, 'read_json_body', side_effect=RequestBodyTimeout), \
                 patch.object(main, 'record_request_usage', new_callable=AsyncMock) as usage, \
                 patch.object(main, 'get_model_route') as route:
                response = await main.chat_completions(request)
            self.assertEqual(response.status_code, 408)
            self.assertIn(b'request_body_timeout', response.body)
            route.assert_not_called()
            if path.endswith('inspect'):
                usage.assert_not_awaited()
                self.assertEqual(response.headers['cache-control'], 'no-store')
            else:
                usage.assert_awaited_once()
        with patch.object(management, 'read_json_body', side_effect=RequestBodyTimeout):
            with self.assertRaises(HTTPException) as error:
                await management.body(request, management.Confirmation)
        self.assertEqual(error.exception.status_code, 408)

    async def test_long_content_length_does_not_raise_parse_error(self):
        request, _ = request_for([b'{}'], [(b'content-length', b'0' * 5000 + b'2')])
        self.assertEqual(await read_json_body(request, 1000), {})

    def test_timeout_configuration_rejects_unbounded_values(self):
        self.assertEqual(configured_body_timeout('1.5'), 1.5)
        for value in ['0', '-1', 'nan', 'inf', '-inf', 'invalid']:
            with self.assertRaises(ValueError):
                configured_body_timeout(value)
