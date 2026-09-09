"""Application-owned HTTP pools with request-scoped borrowed handles."""
from contextlib import asynccontextmanager, AsyncExitStack

import httpx


class BorrowedClient:
    """Adapters close their handle; only application shutdown closes the pool."""
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        pass

    def build_request(self, *args, **kwargs):
        return self.client.build_request(*args, **kwargs)

    async def send(self, *args, **kwargs):
        return await self.client.send(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self.client.post(*args, **kwargs)


class NoCookies(httpx.Cookies):
    def extract_cookies(self, response):
        # Provider cookies must not become shared authentication/session state.
        pass


@asynccontextmanager
async def pooled_adapters():
    from providers.fireworks import FireworksAdapter
    from providers.anthropic import AnthropicAdapter
    async with AsyncExitStack() as stack:
        adapters = {}
        for name, adapter_type in [('fireworks', FireworksAdapter), ('anthropic', AnthropicAdapter)]:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=60,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)))
            client._cookies = NoCookies()
            adapters[name] = adapter_type(client_factory=lambda client=client: BorrowedClient(client))
        yield adapters
