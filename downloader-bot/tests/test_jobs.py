import asyncio

import pytest

from bot.jobs import Busy, Jobs


async def test_same_user_is_busy():
    jobs = Jobs(3)
    async with jobs.slot(1):
        with pytest.raises(Busy):
            async with jobs.slot(1):
                pass
    async with jobs.slot(1):
        pass


async def test_released_after_exception():
    jobs = Jobs(1)
    with pytest.raises(RuntimeError):
        async with jobs.slot(1):
            raise RuntimeError
    async with jobs.slot(1):
        pass
    async with jobs.slot(2):
        pass


async def test_global_limit_makes_others_wait():
    jobs = Jobs(1)
    order = []
    release = asyncio.Event()

    async def first():
        async with jobs.slot(1):
            order.append("a-in")
            await release.wait()
            order.append("a-out")

    async def second():
        async with jobs.slot(2):
            order.append("b-in")

    t1 = asyncio.create_task(first())
    await asyncio.sleep(0)
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.01)
    assert jobs.waiting == 1
    assert order == ["a-in"]
    release.set()
    await asyncio.gather(t1, t2)
    assert order == ["a-in", "a-out", "b-in"]
    assert jobs.waiting == 0


async def test_cancelled_waiter_is_released():
    jobs = Jobs(1)
    async with jobs.slot(1):
        async def wait():
            async with jobs.slot(2):
                pass
        t = asyncio.create_task(wait())
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    assert jobs.waiting == 0
    async with jobs.slot(2):
        pass
