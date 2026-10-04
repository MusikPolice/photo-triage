"""Unhandled errors in requests: logged with their traceback, answered with a 500."""

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class LogUnhandledErrors:
    """ASGI middleware that logs an exception no handler caught, at `ERROR` with its
    traceback, and answers 500 `{"detail": "Internal Server Error"}`.

    The exception stops here, so the server doesn't log it a second time. If the
    response had already started (a stream), it can't be replaced by a 500, so the
    exception goes on to the server, which logs it and drops the connection.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            if response_started:
                raise
            logger.exception("Unhandled error in %s %s", scope["method"], scope["path"])
            response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
            await response(scope, receive, send)
