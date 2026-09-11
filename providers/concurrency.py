"""Per-process bound on active upstream calls, including streamed responses."""
import asyncio
from fastapi.responses import StreamingResponse


class GatewayBusy(Exception):
    pass


class ProviderSlots:
    def __init__(self, limit=5, wait_seconds=2):
        self.semaphore = asyncio.Semaphore(limit)
        self.wait_seconds = wait_seconds

    async def acquire(self):
        try:
            await asyncio.wait_for(self.semaphore.acquire(), self.wait_seconds)
        except asyncio.TimeoutError as error:
            raise GatewayBusy from error
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                self.semaphore.release()
        return release


class SlotStreamingResponse(StreamingResponse):
    def __init__(self, *args, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release
        original = self.body_iterator

        async def tracked():
            try:
                async for chunk in original:
                    yield chunk
            finally:
                release()
        self.body_iterator = tracked()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()


async def limited_send(slots, adapter, request):
    release = await slots.acquire()
    try:
        response = await adapter.send(request)
    except BaseException:
        release()
        raise
    if not response.streaming:
        release()
    return response, release
