"""One shared poller behind every open event stream (plan §7 "Live updates").

The worker is another process, so the API learns of changes by reading the
database. A single `Feed` does that about once a second while any stream is open,
and hands each change to every stream, so the load doesn't grow with open tabs.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable

POLL_S = 1.0
"""How often the feed reads while anyone is listening."""


class Feed[T]:
    def __init__(self, read: Callable[[], T], poll_s: float = POLL_S) -> None:
        """`read` is blocking, so it runs in a thread."""
        self._read = read
        self._poll_s = poll_s
        self._latest: T | None = None
        self._version = 0
        self._changed = asyncio.Condition()
        self._refreshing = asyncio.Lock()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listeners = 0
        self._closed = False

    async def run(self) -> None:
        """Poll until cancelled. Run it for the app's lifetime."""
        self._loop = asyncio.get_running_loop()
        while True:
            if self._listeners:
                await self._refresh()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll_s)
            self._wake.clear()

    def poke(self) -> None:
        """Read again now rather than at the next poll, after a change the API made.
        Safe to call from any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def close(self) -> None:
        """End every stream, so the server can stop. Safe to call from any thread,
        or from a signal handler."""
        self._closed = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._notify()))

    async def updates(self) -> AsyncGenerator[T]:
        """The current value, then each change, until the caller stops iterating or
        the feed is closed."""
        self._listeners += 1
        try:
            await self._refresh()  # what was cached may be old if nobody was listening
            seen = self._version
            assert self._latest is not None
            yield self._latest
            while True:
                async with self._changed:
                    while self._version == seen and not self._closed:
                        await self._changed.wait()
                if self._closed:
                    return
                seen = self._version
                assert self._latest is not None
                yield self._latest
        finally:
            self._listeners -= 1

    async def _refresh(self) -> None:
        async with self._refreshing:  # so an older read can't replace a newer one
            value = await asyncio.to_thread(self._read)
            if value == self._latest:
                return
            self._latest = value
            self._version += 1
        await self._notify()

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()
