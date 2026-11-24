"""The lightbox's API (plan §6.4): an item's details, and its picture or video in a
format every browser shows (`photo_triage.pipeline.viewing`)."""

import asyncio
import contextlib
import datetime as dt
import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from photo_triage.api import items
from photo_triage.db.models import Item, ItemStatus, MediaType, Tag, TagSource
from photo_triage.pipeline import viewing
from photo_triage.settings import Settings

pytestmark = pytest.mark.anyio

NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)


@pytest.fixture
def add_item(migrated: Engine, library: Path) -> Callable[..., int]:
    """Register a file in the library as an item, and return its id."""

    def add(relative: str, media_type: MediaType = MediaType.PHOTO, **fields: Any) -> int:
        path = library / relative
        size = path.stat().st_size if path.exists() else 1
        with Session(migrated) as session, session.begin():
            item = Item(
                content_hash="0" * 64,
                path=relative,
                media_type=media_type,
                file_size_bytes=size,
                file_mtime_ns=0,
                first_seen_at=NOW,
                last_seen_at=NOW,
                **fields,
            )
            session.add(item)
            session.flush()
            return item.id

    return add


def hevc(path: Path, seconds: float = 1, size: str = "320x240") -> Path:
    """A short HEVC video with AAC sound, as an iPhone records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
            "-f", "lavfi", "-i", f"sine=duration={seconds}",
            "-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1",
            "-x265-params", "log-level=error",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def probe(data: bytes, tmp_path: Path) -> dict[str, Any]:
    """What ffprobe says about a video's bytes."""
    path = tmp_path / "probed.mp4"
    path.write_bytes(data)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


# --- Details -------------------------------------------------------------------------


async def test_details_say_what_the_lightbox_shows(
    client: AsyncClient, add_item: Callable[..., int], migrated: Engine
) -> None:
    item_id = add_item(
        "metadata/keywords.jpg",
        taken_at_local=dt.datetime(2019, 4, 15, 10, 20, 30),
        width=640,
        height=480,
    )
    with Session(migrated) as session, session.begin():
        for tag in ("zebra", "Tofino", "apple"):
            session.add(Tag(item_id=item_id, source=TagSource.IMPORTED, tag=tag, created_at=NOW))

    response = await client.get(f"/api/items/{item_id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": item_id,
        "path": "metadata/keywords.jpg",
        "media_type": "photo",
        "taken_at_local": "2019-04-15T10:20:30",
        "width": 640,
        "height": 480,
        "duration_s": None,
        "file_size_bytes": (Path("photos/metadata/keywords.jpg")).stat().st_size,
        "damaged": False,
        "tags": ["apple", "Tofino", "zebra"],
    }


@pytest.mark.parametrize("status", [ItemStatus.TRASHED, ItemStatus.MISSING, ItemStatus.PURGED])
async def test_an_item_that_is_not_in_the_library_is_not_found(
    client: AsyncClient, add_item: Callable[..., int], status: ItemStatus
) -> None:
    item_id = add_item("scenes/scene-01.jpg", status=status)

    assert (await client.get(f"/api/items/{item_id}")).status_code == 404
    assert (await client.get(f"/api/items/{item_id}/view")).status_code == 404


async def test_an_unknown_item_is_not_found(client: AsyncClient, migrated: Engine) -> None:
    assert (await client.get("/api/items/999")).status_code == 404
    assert (await client.get("/api/items/999/view")).status_code == 404


# --- Photos --------------------------------------------------------------------------


async def test_a_jpeg_is_served_as_it_is(
    client: AsyncClient, add_item: Callable[..., int], library: Path
) -> None:
    item_id = add_item("scenes/scene-01.jpg")

    response = await client.get(f"/api/items/{item_id}/view")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == (library / "scenes/scene-01.jpg").read_bytes()


async def test_a_heic_comes_back_as_a_jpeg_of_the_whole_picture(
    client: AsyncClient, add_item: Callable[..., int], library: Path
) -> None:
    item_id = add_item("heic/scene.heic")

    response = await client.get(f"/api/items/{item_id}/view")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    with (
        Image.open(io.BytesIO(response.content)) as jpeg,
        Image.open(library / "heic/scene.heic") as original,
    ):
        assert jpeg.format == "JPEG"
        assert jpeg.size == original.size


async def test_a_heic_is_converted_again_only_if_it_changed(
    client: AsyncClient, add_item: Callable[..., int]
) -> None:
    item_id = add_item("heic/scene.heic")
    first = await client.get(f"/api/items/{item_id}/view")

    again = await client.get(
        f"/api/items/{item_id}/view", headers={"If-None-Match": first.headers["etag"]}
    )

    assert again.status_code == 304


async def test_a_photo_that_cannot_be_read_is_unprocessable(
    client: AsyncClient, add_item: Callable[..., int], library: Path
) -> None:
    (library / "heic/broken.heic").write_bytes(b"not a picture")
    item_id = add_item("heic/broken.heic")

    assert (await client.get(f"/api/items/{item_id}/view")).status_code == 422


async def test_a_file_that_is_gone_is_not_found(
    client: AsyncClient, add_item: Callable[..., int], library: Path
) -> None:
    item_id = add_item("scenes/scene-01.jpg")
    (library / "scenes/scene-01.jpg").unlink()

    assert (await client.get(f"/api/items/{item_id}/view")).status_code == 404


async def test_a_path_outside_the_library_is_not_served(
    client: AsyncClient, add_item: Callable[..., int], settings: Settings
) -> None:
    (settings.photo_dir.parent / "secret.jpg").write_bytes(b"\xff\xd8 not the library's")
    item_id = add_item("../secret.jpg")

    assert (await client.get(f"/api/items/{item_id}/view")).status_code == 404


# --- Videos --------------------------------------------------------------------------


async def test_an_h264_video_is_served_as_it_is_with_ranges(
    client: AsyncClient, add_item: Callable[..., int], library: Path
) -> None:
    item_id = add_item("video/clip.mov", MediaType.VIDEO)
    original = (library / "video/clip.mov").read_bytes()

    response = await client.get(f"/api/items/{item_id}/view", headers={"Range": "bytes=0-99"})

    assert response.status_code == 206
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-range"] == f"bytes 0-99/{len(original)}"
    assert response.content == original[:100]


async def test_an_hevc_video_comes_back_as_h264(
    client: AsyncClient, add_item: Callable[..., int], library: Path, tmp_path: Path
) -> None:
    hevc(library / "video/iphone.mov")
    item_id = add_item("video/iphone.mov", MediaType.VIDEO)

    response = await client.get(f"/api/items/{item_id}/view")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    streams = {s["codec_type"]: s for s in probe(response.content, tmp_path)["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == (320, 240)
    assert streams["audio"]["codec_name"] == "aac"


@pytest.mark.parametrize(
    ("size", "scaled"),
    [("1920x1080", (1280, 720)), ("1080x1920", (720, 1280)), ("320x240", (320, 240))],
)
async def test_a_converted_video_is_at_most_720p(
    client: AsyncClient,
    add_item: Callable[..., int],
    library: Path,
    tmp_path: Path,
    size: str,
    scaled: tuple[int, int],
) -> None:
    hevc(library / "video/big.mov", seconds=0.2, size=size)
    item_id = add_item("video/big.mov", MediaType.VIDEO)

    response = await client.get(f"/api/items/{item_id}/view")

    video = next(
        s for s in probe(response.content, tmp_path)["streams"] if s["codec_type"] == "video"
    )
    assert (video["width"], video["height"]) == scaled


@pytest.mark.parametrize(
    ("codecs", "playable"),
    [
        (viewing.Codecs("h264", "aac"), True),
        (viewing.Codecs("h264", None), True),
        (viewing.Codecs("h264", "pcm_s16le"), False),
        (viewing.Codecs("hevc", "aac"), False),
        (viewing.Codecs(None, None), False),
    ],
)
def test_only_h264_with_browser_sound_plays_as_it_is(
    codecs: viewing.Codecs, playable: bool
) -> None:
    assert codecs.playable is playable


# --- A disconnect stops the conversion ----------------------------------------------


@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
async def test_a_disconnect_mid_conversion_stops_ffmpeg(
    app: FastAPI,
    add_item: Callable[..., int],
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    asgi_spec: str,
) -> None:
    """Before ASGI 2.4, Starlette listens for the disconnect message; from 2.4, the
    server's `send` fails once the browser has gone (uvicorn's does)."""
    hevc(library / "video/long.mov", seconds=20)
    item_id = add_item("video/long.mov", MediaType.VIDEO)
    # Read at the video's own pace, so it's still converting when the browser goes.
    command = viewing.conversion_command
    monkeypatch.setattr(viewing, "conversion_command", lambda p: ["ffmpeg", "-re", *command(p)[1:]])
    started: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def recorded(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(items.asyncio, "create_subprocess_exec", recorded)

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "http.request", "body": b"", "more_body": False})
    streaming = asyncio.Event()
    gone = asyncio.Event()

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: dict[str, Any]) -> None:
        if gone.is_set():
            # As uvicorn does: from 2.4 a send fails once the browser has gone; before
            # it, the send is dropped, so a chunk that races the disconnect is harmless.
            if asgi_spec == "2.4":
                raise OSError("The browser has gone")
            return
        if message["type"] == "http.response.body" and message.get("body"):
            streaming.set()

    path = f"/api/items/{item_id}/view"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": asgi_spec},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("test", 1234),
    }
    request = asyncio.create_task(app(scope, receive, send))  # type: ignore[arg-type]
    await asyncio.wait_for(streaming.wait(), timeout=30)
    [ffmpeg] = started
    assert ffmpeg.returncode is None  # still converting

    gone.set()
    await inbox.put({"type": "http.disconnect"})
    # ASGI 2.4 raises from the failed send, which the server would see.
    with contextlib.suppress(ClientDisconnect):
        await asyncio.wait_for(request, timeout=10)

    assert ffmpeg.returncode is not None
    assert ffmpeg.returncode != 0  # killed, not finished
