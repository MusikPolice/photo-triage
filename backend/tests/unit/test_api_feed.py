"""The shared poller behind the event streams (plan §7 "Live updates")."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from photo_triage.api.feed import Feed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Source:
    def __init__(self) -> None:
        self.value = 0
        self.reads = 0

    def __call__(self) -> int:
        self.reads += 1
        return self.value


async def _next(updates: AsyncIterator[int]) -> int:
    return await asyncio.wait_for(anext(updates), timeout=5)


async def test_streams_share_one_read_per_poll_and_each_sees_every_change() -> None:
    source = Source()
    feed = Feed(source, poll_s=0.01)
    polling = asyncio.create_task(feed.run())
    streams = [feed.updates() for _ in range(10)]
    try:
        assert [await _next(s) for s in streams] == [0] * 10
        reads_at_start = source.reads

        await asyncio.sleep(0.2)  # about 20 polls

        assert source.reads - reads_at_start < 40  # not one read per stream per poll

        source.value = 1
        assert [await _next(s) for s in streams] == [1] * 10
    finally:
        polling.cancel()
        for s in streams:
            await s.aclose()


async def test_nothing_is_read_while_nobody_listens() -> None:
    source = Source()
    feed = Feed(source, poll_s=0.01)
    polling = asyncio.create_task(feed.run())

    await asyncio.sleep(0.1)

    polling.cancel()
    assert source.reads == 0


async def test_a_poke_reads_at_once() -> None:
    source = Source()
    feed = Feed(source, poll_s=60)
    polling = asyncio.create_task(feed.run())
    await asyncio.sleep(0)  # let it start
    updates = feed.updates()
    try:
        assert await _next(updates) == 0

        source.value = 1
        feed.poke()

        assert await _next(updates) == 1
    finally:
        polling.cancel()
        await updates.aclose()
