"""Fill an empty database with a synthetic library for the map: items, a layout, 64 px
thumbnails and the atlases (#119, plan §6.4 performance target). Dev only.

    python seed_map.py N

`just map-stack` runs it inside the image, against a scratch DATA_DIR. It reads
DATA_DIR and PHOTO_DIR from the environment, and refuses a database that already
has items or a library that has files, so it can't mix made-up items into a real
one.

The items fall into clusters, as CLIP groups photos, with capture years and media
types for the colour overlays. Each thumbnail is a small landscape with noise, so
the atlas sheets come out the size real photos' do (`NOISE`). One item in
50 gets no thumbnail and stays a dot, as an item whose thumbnail failed does.
"""

import colorsys
import datetime as dt
import sys
import time

import numpy as np
import sqlalchemy as sa
from PIL import Image, ImageDraw
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.engine import open_database
from photo_triage.db.models import Item, ItemStatus, Layout, MediaType
from photo_triage.pipeline import atlases
from photo_triage.pipeline.thumbnails import Thumbnail, encode, thumbnail_path
from photo_triage.settings import load_settings

CLUSTERS = 40
SPREAD = 0.035
"""Each cluster's standard deviation, on a map 1 wide."""
NO_THUMBNAIL_EVERY = 50
VIDEO_FRACTION = 0.08
UNDATED_FRACTION = 0.03
SHAPES = [(64, 48), (48, 64), (64, 64), (64, 36)]
NOISE = 10
"""The noise's standard deviation, in levels: a sheet of these takes 1.6 kB a cell,
as one of 256 photos from the tier-2 sample does (1.65 kB)."""
NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)
BATCH = 5000


def main(count: int) -> None:
    settings = load_settings()
    if any(settings.photo_dir.iterdir()):
        sys.exit(f"{settings.photo_dir} isn't empty: seed only a scratch stack")
    engine = open_database(settings.data_dir)
    sessions = sessionmaker(engine)
    with sessions() as session:
        if session.scalar(sa.select(sa.func.count()).select_from(Item)):
            sys.exit(f"The database in {settings.data_dir} has items: seed only a scratch stack")

    started_s = time.monotonic()
    rng = np.random.default_rng(119)
    centres = rng.uniform(0.05, 0.95, size=(CLUSTERS, 2))
    hues = rng.uniform(0, 1, CLUSTERS)
    years = rng.integers(2004, 2026, CLUSTERS)
    cluster = rng.integers(0, CLUSTERS, count)
    xy = centres[cluster] + rng.normal(scale=SPREAD, size=(count, 2))
    xy -= xy.min(0)
    xy /= xy.max()

    with sessions.begin() as session:
        for start in range(0, count, BATCH):
            rows = []
            for i in range(start, min(start + BATCH, count)):
                year = int(np.clip(years[cluster[i]] + rng.normal(scale=1.5), 2004, 2025))
                undated = rng.random() < UNDATED_FRACTION
                video = rng.random() < VIDEO_FRACTION
                rows.append(
                    {
                        "id": i + 1,
                        "content_hash": f"{i:064x}",
                        "path": f"synthetic/{i}.jpg",
                        "media_type": MediaType.VIDEO if video else MediaType.PHOTO,
                        "status": ItemStatus.ACTIVE,
                        "file_size_bytes": 1,
                        "file_mtime_ns": 0,
                        "first_seen_at": NOW,
                        "last_seen_at": NOW,
                        "taken_at_local": None
                        if undated
                        else dt.datetime(year, int(rng.integers(1, 13)), 15, 12),
                    }
                )
            session.execute(sa.insert(Item), rows)
        session.execute(
            sa.insert(Layout),
            [
                {"layout_version": 1, "item_id": i + 1, "x": float(x), "y": float(y)}
                for i, (x, y) in enumerate(xy)
            ],
        )
    print(f"{count} items and a layout in {time.monotonic() - started_s:.0f} s", flush=True)

    started_s = time.monotonic()
    for i in range(count):
        if i % NO_THUMBNAIL_EVERY == 0:
            continue
        hue = float(hues[cluster[i]] + rng.normal(scale=0.03))
        width, height = SHAPES[int(rng.integers(0, len(SHAPES)))]
        path = thumbnail_path(settings.derived_dir, i + 1, Thumbnail.MAP)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode(_landscape(rng, width, height, hue)))
    print(f"Thumbnails in {time.monotonic() - started_s:.0f} s", flush=True)

    built = atlases.build(sessions, settings.derived_dir)
    print(built, flush=True)
    with Session(engine) as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Layout)) == count
    engine.dispose()


def _landscape(rng: np.random.Generator, width: int, height: int, hue: float) -> Image.Image:
    def rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(h % 1, s, v)
        return int(r * 255), int(g * 255), int(b * 255)

    image = Image.new("RGB", (width, height), rgb(hue, 0.35, 0.95))
    draw = ImageDraw.Draw(image)
    horizon = int(height * rng.uniform(0.45, 0.7))
    draw.rectangle((0, horizon, width, height), fill=rgb(hue + 0.08, 0.6, 0.55))
    r = int(rng.integers(5, 12))
    cx = int(rng.integers(r, width - r))
    cy = int(rng.integers(r, max(r + 1, horizon)))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb(hue + 0.5, 0.7, 0.85))
    noise = rng.normal(0, NOISE, (height, width, 3))
    pixels = np.clip(np.asarray(image, np.float32) + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(pixels)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100_000)
