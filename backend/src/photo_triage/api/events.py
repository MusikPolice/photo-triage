"""`GET /api/events`: Server-Sent Events for open tabs (plan §7 "Live updates").

Each stream gets the current activity at once, then the new activity whenever it
changes, as unnamed (`message`) events with an `Activity` as JSON. FastAPI adds a
keep-alive comment when nothing has been sent for 15 s.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse

from photo_triage.api.activity import Activity, get_feed
from photo_triage.api.feed import Feed

router = APIRouter()


@router.get("/events", response_class=EventSourceResponse)
async def events(feed: Annotated[Feed[Activity], Depends(get_feed)]) -> AsyncIterator[Activity]:
    async for activity in feed.updates():
        yield activity
