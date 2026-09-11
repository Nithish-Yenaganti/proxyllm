import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from providers.concurrency import GatewayBusy, ProviderSlots, limited_send, SlotStreamingResponse


class ConcurrencyTests(IsolatedAsyncioTestCase):
    async def test_five_slots_then_timeout(self):
        slots = ProviderSlots(wait_seconds=.01)
        releases = [await slots.acquire() for _ in range(5)]
        with self.assertRaises(GatewayBusy):
            await slots.acquire()
        for release in releases:
            release()
            release()
        self.assertEqual(slots.semaphore._value,5)

    async def test_waiter_starts_after_release(self):
        slots = ProviderSlots(limit=1)
        release = await slots.acquire()
        waiting = asyncio.create_task(slots.acquire())
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        release()
        (await waiting)()

    async def test_error_and_complete_release(self):
        slots = ProviderSlots(limit=1)
        adapter = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError('test')))
        with self.assertRaises(RuntimeError):
            await limited_send(slots,adapter,None)
        adapter.send = AsyncMock(return_value=SimpleNamespace(streaming=False))
        await limited_send(slots,adapter,None)
        self.assertEqual(slots.semaphore._value,1)

    async def test_stream_slot_held_until_asgi_finishes(self):
        slots = ProviderSlots(limit=1)
        adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(streaming=True)))
        _, release = await limited_send(slots,adapter,None)
        self.assertEqual(slots.semaphore._value,0)
        async def chunks():
            yield b'test'
        response = SlotStreamingResponse(chunks(),release=release)
        await response({'type':'http','asgi':{'spec_version':'2.4'}},AsyncMock(),AsyncMock())
        self.assertEqual(slots.semaphore._value,1)

    async def test_cancelled_provider_releases(self):
        slots = ProviderSlots(limit=1)
        adapter = SimpleNamespace(send=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await limited_send(slots,adapter,None)
        self.assertEqual(slots.semaphore._value,1)

    async def test_cancelled_waiter_does_not_take_slot(self):
        slots = ProviderSlots(limit=1)
        release = await slots.acquire()
        waiter = asyncio.create_task(slots.acquire())
        await asyncio.sleep(.001)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release()
        self.assertEqual(slots.semaphore._value,1)

    async def test_downstream_send_failure_releases_stream_slot(self):
        slots = ProviderSlots(limit=1)
        release = await slots.acquire()
        async def chunks():
            yield b'test'
        response = SlotStreamingResponse(chunks(),release=release)
        with self.assertRaises(RuntimeError):
            await response({'type':'http','asgi':{'spec_version':'2.4'}},AsyncMock(),AsyncMock(side_effect=RuntimeError('send failed')))
        self.assertEqual(slots.semaphore._value,1)
