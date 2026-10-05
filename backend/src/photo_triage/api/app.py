import asyncio
import contextlib
import signal
import threading
from collections.abc import AsyncGenerator, Generator
from types import FrameType

from fastapi import FastAPI
from sqlalchemy.orm import Session

from photo_triage.api import activity, events, health
from photo_triage.api.errors import LogUnhandledErrors
from photo_triage.api.feed import POLL_S, Feed
from photo_triage.db.engine import open_database
from photo_triage.settings import Settings, load_settings
from photo_triage.worker.clock import Clock, running_from, utc_now


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    real_clock: Clock = utc_now,
    poll_s: float = POLL_S,
) -> FastAPI:
    """Build the API. Settings come from the environment unless given. `clock` is
    the app's time, set by `FAKE_NOW` unless given. `real_clock` judges the worker's
    heartbeat. `poll_s` is how often open event streams look for changes."""
    settings = settings if settings is not None else load_settings()
    if clock is None:
        clock = utc_now if settings.fake_now is None else running_from(settings.fake_now)
    engine = open_database(settings.data_dir)

    def read() -> activity.Activity:
        with Session(engine) as session:
            return activity.read_activity(session, settings, clock(), real_clock())

    feed = Feed(read, poll_s)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        polling = asyncio.create_task(feed.run())
        try:
            with _closing_on_exit_signals(feed):
                yield
        finally:
            polling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling
            engine.dispose()

    app = FastAPI(
        title="photo-triage",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.clock = clock
    app.state.real_clock = real_clock
    app.state.feed = feed
    app.add_middleware(LogUnhandledErrors)
    app.include_router(health.router, prefix="/api")
    app.include_router(activity.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    return app


@contextlib.contextmanager
def _closing_on_exit_signals(feed: Feed[activity.Activity]) -> Generator[None]:
    """Close the event streams when the server is told to stop, then pass the signal
    on to the server's own handler.

    Uvicorn waits for open requests to finish before it stops or reloads, and an
    event stream never finishes by itself. Browsers reconnect once the server is
    back. Signals can only be handled in the main thread, which is where uvicorn
    runs the app, except in tests.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def close_then_pass_on(signum: int, frame: FrameType | None) -> None:
        feed.close()
        handler = previous[signal.Signals(signum)]
        if callable(handler):
            handler(signum, frame)

    for sig in previous:
        signal.signal(sig, close_then_pass_on)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
