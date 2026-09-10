"""Boundary checks use synthetic ASGI chunks, never real providers."""
import unittest
from unittest.mock import AsyncMock, patch
from starlette.requests import Request
from api.request_body import RequestTooLarge, configured_body_limit, read_json_body
from api import main


def request_for(chunks, headers=()):
    pending = list(chunks)
    async def receive():
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                       "headers": list(headers)}, receive)
    request.state.virtual_key = {"id": 1}
    return request, pending


class RequestBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_boundary(self):
        request, _ = request_for([b'{"a":', b'1}'])
        self.assertEqual(await read_json_body(request, 7), {"a": 1})

    async def test_oversized_header_reads_nothing(self):
        request, pending = request_for([b'{}'], [(b'content-length', b'8')])
        with self.assertRaises(RequestTooLarge):
            await read_json_body(request, 7)
        self.assertEqual(len(pending), 1)

    async def test_missing_or_understated_header_cannot_bypass(self):
        for headers in [(), [(b'content-length', b'1')]]:
            request, pending = request_for([b'1234', b'5678', b'never read'], headers)
            with self.assertRaises(RequestTooLarge):
                await read_json_body(request, 7)
            self.assertEqual(pending, [b'never read'])

    async def test_endpoint_rejects_before_routing(self):
        request, _ = request_for([b'12345678'])
        with patch.object(main, 'MAX_REQUEST_BYTES', 7), \
             patch.object(main, 'record_request_usage', new_callable=AsyncMock) as usage, \
             patch.object(main, 'get_model_route') as route:
            response = await main.chat_completions(request)
        self.assertEqual(response.status_code, 413)
        self.assertIn(b'request_too_large', response.body)
        route.assert_not_called()
        usage.assert_awaited_once()

    def test_configuration(self):
        self.assertEqual(configured_body_limit('5000000'), 5000000)
        for value in ['0', '-1', 'abc']:
            with self.assertRaises(ValueError):
                configured_body_limit(value)
