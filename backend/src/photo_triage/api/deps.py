"""FastAPI dependencies shared by every router."""

from typing import Annotated, cast

from fastapi import Depends, Request

from photo_triage.settings import Settings

ANONYMOUS = "anonymous"


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


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
