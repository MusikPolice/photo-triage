"""Whether the worker is claiming jobs, and why not (plan §6.11, §7).

The worker and the API work it out the same way, from `worker_controls` and the
quiet hours, so the Activity page agrees with what the worker does. Only the API
and the `pause`/`resume` commands add `STOPPED`, from the heartbeat
(`observed_state`).
"""

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from photo_triage.quiet_hours import QuietHours
from photo_triage.worker import controls, heartbeat


class WorkerStatus(StrEnum):
    RUNNING = "running"
    """Claiming jobs, or waiting for one. Some stages may be paused."""
    PAUSED = "paused"
    """Paused globally."""
    QUIET = "quiet"
    """In quiet hours, until `WorkerState.quiet_until_at`."""
    STOPPED = "stopped"
    """The worker process isn't running: it stopped, crashed or was killed."""


@dataclass(frozen=True)
class WorkerState:
    status: WorkerStatus = WorkerStatus.RUNNING
    quiet_until_at: dt.datetime | None = None
    paused_stages: frozenset[str] = field(default_factory=frozenset[str])

    def describe(self, zone: ZoneInfo) -> str:
        match self.status:
            case WorkerStatus.PAUSED:
                text = "Worker paused"
            case WorkerStatus.QUIET:
                assert self.quiet_until_at is not None
                local = self.quiet_until_at.astimezone(zone)
                text = (
                    f"Quiet hours until {local:%a %H:%M %Z} "
                    f"({self.quiet_until_at:%Y-%m-%dT%H:%M:%SZ})"
                )
            case WorkerStatus.RUNNING:
                text = "Worker running"
            case WorkerStatus.STOPPED:
                text = "Worker stopped"
        if self.paused_stages:
            text += f"; paused stages: {', '.join(sorted(self.paused_stages))}"
        return text


def read_state(
    session: Session, quiet_hours: QuietHours | None, zone: ZoneInfo, now: dt.datetime
) -> WorkerState:
    """The state at `now`: paused globally, else in quiet hours, else running. Stages
    paused one by one are listed in every state."""
    paused = controls.paused_scopes(session)
    paused_stages = frozenset(paused - {controls.GLOBAL})
    if controls.GLOBAL in paused:
        return WorkerState(WorkerStatus.PAUSED, paused_stages=paused_stages)
    if quiet_hours is not None:
        until = quiet_hours.quiet_until(now, zone)
        if until is not None:
            return WorkerState(WorkerStatus.QUIET, until, paused_stages)
    return WorkerState(paused_stages=paused_stages)


def observed_state(
    session: Session,
    quiet_hours: QuietHours | None,
    zone: ZoneInfo,
    now: dt.datetime,
    real_now: dt.datetime,
) -> WorkerState:
    """`read_state`, but `STOPPED` if the worker process isn't running at `real_now`
    (see `photo_triage.worker.heartbeat`). For anything other than the worker."""
    state = read_state(session, quiet_hours, zone, now)
    if not heartbeat.is_alive(heartbeat.last(session), real_now):
        return replace(state, status=WorkerStatus.STOPPED, quiet_until_at=None)
    return state
