"""`GET /api/events`, read from a real server (plan §7 "Live updates").

The in-process test clients wait for the whole response, and an event stream never
ends, so these tests run uvicorn in a thread.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.__main__ import SHUTDOWN_TIMEOUT_S
from photo_triage.api.app import create_app
from photo_triage.settings import Settings
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, Stage

POLL_S = 0.05


@pytest.fixture
def base_url(settings: Settings, migrated: Engine) -> Iterator[str]:
    app = create_app(settings, poll_s=POLL_S)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_config=None, timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline_s = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline_s, "the server didn't start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "the server didn't stop"
    sock.close()


class Events:
    """The `data` of each event on a stream, as JSON."""

    def __init__(self, response: httpx.Response) -> None:
        self._lines = response.iter_lines()

    def next(self) -> dict[str, Any]:
        data: list[str] = []
        for line in self._lines:
            if line.startswith("data: "):
                data.append(line.removeprefix("data: "))
            elif line == "" and data:
                return json.loads("\n".join(data))
        raise AssertionError("the stream ended")


def _stage(activity: dict[str, Any], stage: str) -> dict[str, Any]:
    [row] = [row for row in activity["stages"] if row["stage"] == stage]
    return row


def test_a_snapshot_then_an_event_when_a_job_changes_state(base_url: str, migrated: Engine) -> None:
    queue = JobQueue()
    with Session(migrated) as session, session.begin():
        queue.enqueue(session, Stage.LAYOUT)

    with httpx.stream("GET", f"{base_url}/api/events", timeout=10) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = Events(response)

        first = events.next()
        assert _stage(first, "layout")["pending"] == 1

        # The worker is another process: the stream sees the change in the database.
        Worker(migrated, {Stage.LAYOUT: lambda job: None}, queue=queue).drain()

        second = events.next()
        assert (_stage(second, "layout")["pending"], _stage(second, "layout")["done"]) == (0, 1)


def test_a_pause_is_pushed_to_every_open_stream(base_url: str) -> None:
    with (
        httpx.stream("GET", f"{base_url}/api/events", timeout=10) as one,
        httpx.stream("GET", f"{base_url}/api/events", timeout=10) as two,
    ):
        streams = [Events(one), Events(two)]
        for events in streams:
            assert events.next()["worker"]["paused"] is False

        assert httpx.post(f"{base_url}/api/worker/pause").status_code == 200

        for events in streams:
            assert events.next()["worker"]["paused"] is True


def test_open_streams_end_when_the_server_is_told_to_stop(
    settings: Settings, migrated: Engine, tmp_path: Path
) -> None:
    """Uvicorn waits for open requests before it stops or reloads, and an event stream
    never ends by itself. So the app ends its streams on SIGINT or SIGTERM."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PHOTO_DIR": str(settings.photo_dir),
        "TRASH_DIR": str(settings.trash_dir),
        "DATA_DIR": str(settings.data_dir),
        "APP_PORT": str(port),
    }
    output = tmp_path / "api.out"
    with output.open("w") as out:
        process = subprocess.Popen(
            [sys.executable, "-m", "photo_triage.api"], env=env, stdout=out, stderr=out
        )
    try:
        base_url = f"http://127.0.0.1:{port}"
        deadline_s = time.monotonic() + 30
        while True:
            assert process.poll() is None, output.read_text()
            assert time.monotonic() < deadline_s, "the API didn't start"
            try:
                httpx.get(f"{base_url}/api/health")
                break
            except httpx.ConnectError:
                time.sleep(0.05)
        with httpx.stream("GET", f"{base_url}/api/events", timeout=10) as response:
            events = Events(response)
            events.next()

            process.send_signal(signal.SIGINT)

            assert process.wait(timeout=SHUTDOWN_TIMEOUT_S - 1) == 0
            with pytest.raises(AssertionError, match="the stream ended"):
                events.next()
    finally:
        process.kill()
        process.wait()
    log = output.read_text()
    assert "Finished server process" in log
    assert "ERROR" not in log
