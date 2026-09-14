"""Verify the real application's public readiness probe, without live providers."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from api import main


class HealthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_is_public_and_does_not_call_provider(self):
        # ASGITransport does not start lifespan or open provider connections.
        with patch.object(main, 'get_provider_adapter') as adapter, \
             patch.object(main, 'record_request_usage', new_callable=AsyncMock) as usage:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url='http://test') as client:
                response = await client.get('/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), main.read_root())
            adapter.assert_not_called()
            usage.assert_not_awaited()
