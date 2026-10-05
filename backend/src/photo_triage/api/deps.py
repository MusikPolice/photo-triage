"""FastAPI dependencies shared by every router."""

from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.settings import Settings
from photo_triage.worker.clock import Clock

ANONYMOUS = "anonymous"


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_engine(request: Request) -> Engine:
    return cast(Engine, request.app.state.engine)


def get_session(engine: Annotated[Engine, Depends(get_engine)]) -> Iterator[Session]:
    """A session for the request. Endpoints that write open a transaction with
    `session.begin()`, so the change is committed before they respond."""
    with Session(engine) as session:
        yield session


def get_clock(request: Request) -> Clock:
    """The time for `*_at` columns and quiet hours. `FAKE_NOW` sets it in dev, as it
    does the worker's."""
    return cast(Clock, request.app.state.clock)


def get_real_clock(request: Request) -> Clock:
    """The real time, even with `FAKE_NOW`, for the worker's heartbeat
    (`photo_triage.worker.heartbeat`)."""
    return cast(Clock, request.app.state.real_clock)


def current_actor(settings: Annotated[Settings, Depends(get_settings)]) -> str:
    """Who is making this request (plan §10).

    The only place identity is resolved: every endpoint that records a human
    decision takes its actor from here, so changing `AUTH_MODE` needs no other
    code. With `AUTH_MODE=none` everyone is anonymous.
    """
    match settings.auth_mode:
        case "none":
            return ANONYMOUS


Actor = Annotated[str, Depends(current_actor)]
DbSession = Annotated[Session, Depends(get_session)]
AppClock = Annotated[Clock, Depends(get_clock)]
