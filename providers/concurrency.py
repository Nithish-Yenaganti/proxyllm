"""Per-process bound on active upstream calls, including streamed responses."""
import asyncio
import anyio
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
    def __init__(self, *args, release, close=None, total_seconds=300,
                 write_seconds=30, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release
        self.close = close
        self.total_seconds = total_seconds
        self.write_seconds = write_seconds
        self.original_iterator = self.body_iterator

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            with anyio.fail_after(self.write_seconds):
                await send(message)

        try:
            with anyio.fail_after(self.total_seconds):
                await super().__call__(scope, receive, bounded_send)
        finally:
            # A failed downstream write can leave the iterator suspended at yield.
            # Closing it runs accounting and adapter cleanup before releasing the slot.
            try:
                with anyio.CancelScope(shield=True):
                    close_iterator = getattr(self.original_iterator, "aclose", None)
                    try:
                        if close_iterator is not None:
                            await close_iterator()
                    finally:
                        # Also covers failure while sending headers, before iteration.
                        if self.close is not None:
                            await self.close()
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
