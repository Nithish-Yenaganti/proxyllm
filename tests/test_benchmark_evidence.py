"""Check evidence classification without provider calls."""
import unittest
import httpx
from benchmarks.evidence import measure_request, provenance


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def measure(self, text, status=200, stream=True):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=text))) as client:
            return await measure_request(client, 'http://mock.test', {}, {'stream': stream})

    async def test_stream_first_content_and_done(self):
        result = await self.measure('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                                    'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                                    'data: [DONE]\n\n')
        self.assertTrue(result['complete'])
        self.assertIsNotNone(result['ttft_ms'])
        self.assertNotIn('hello', str(result))

    async def test_role_only_has_no_ttft(self):
        result = await self.measure('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]\n\n')
        self.assertIsNone(result['ttft_ms'])

    async def test_truncated_stream_not_successful(self):
        result = await self.measure('data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
        self.assertFalse(result['complete'])

    async def test_error_event_not_successful(self):
        result = await self.measure('data: {"error":"failed"}\n\ndata: [DONE]\n\n')
        self.assertFalse(result['complete'])

    async def test_rejections_separate(self):
        for status in (429, 503):
            result = await self.measure('{}', status)
            self.assertEqual(result['status'], str(status))
            self.assertFalse(result['complete'])

    def test_provenance(self):
        result = provenance()
        self.assertIn('commit', result)
        self.assertIn('dirty', result)
