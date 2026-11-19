"""The `atlases` batch stage: the map's 64 px thumbnails packed into sheets (plan §6.2).

Each layout version gets one set of 4096 px square JPEG sheets, 4,096 64 px cells to
a sheet, in `DATA_DIR/derived/atlases/<layout version>/`: `0.jpg`, `1.jpg`, ... and
`manifest.bin`, which says where each item is. Items are placed along a Hilbert
curve over their place on the map, so neighbours on the map share a sheet, and a
zoomed-in view needs only a few. A thumbnail that isn't square sits centred in its
cell. An item with no 64 px thumbnail gets no cell, and the map draws it as a dot.

The manifest is little-endian, like the map's points (`photo_triage.api.map_points`):

    offset  size  field
    0       4     magic, b"PTAT"
    4       2     u16 format version, `FORMAT_VERSION`
    6       2     u16 cell size in pixels, `CELL_SIZE`
    8       4     u32 layout version
    12      4     u32 N, the number of items with a cell
    16      2     u16 number of sheets
    18      2     u16 sheet size in pixels, `SHEET_SIZE`
    20      4N    u32 item ids, ascending
    20+4N   2N    u16 sheet
    20+6N   2N    u16 cell within the sheet: row `cell // CELLS_PER_ROW`, column
                  `cell % CELLS_PER_ROW`, counting from the top left
    20+8N   N     u8 thumbnail width in pixels
    20+9N   N     u8 thumbnail height
                  The thumbnail's top left is ((CELL_SIZE - width) // 2,
                  (CELL_SIZE - height) // 2) into its cell.

A set is drawn into a staging folder beside the others, with the manifest last, and
swapped in whole (`photo_triage.files.replace_derived`); then the older sets are
removed. So the API always finds complete sets, and the one it serves changes in a
single step.
"""

import logging
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sqlalchemy as sa
from numpy.typing import NDArray
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemStatus, Layout
from photo_triage.files import remove_derived, replace_derived
from photo_triage.pipeline.thumbnails import MAP_SIZE, Thumbnail, thumbnail_path
from photo_triage.worker.atlas_schedule import MANIFEST, atlas_root, set_path
from photo_triage.worker.layout_schedule import latest_version

logger = logging.getLogger(__name__)

SHEET_SIZE = 4096
CELL_SIZE = MAP_SIZE
CELLS_PER_ROW = SHEET_SIZE // CELL_SIZE
CELLS_PER_SHEET = CELLS_PER_ROW * CELLS_PER_ROW
MAGIC = b"PTAT"
FORMAT_VERSION = 1
HEADER = struct.Struct("<4sHHIIHH")
HILBERT_ORDER = 16
"""Places are rounded to a 2^16 grid along the curve: far finer than any cell."""
_JPEG_QUALITY = 85
"""With full-resolution colour (4:4:4): halving it smears the colours of detailed
64 px thumbnails, and across the edges of their cells."""
_STAGING_PREFIX = ".staging-"


@dataclass(frozen=True)
class Cell:
    item_id: int
    sheet: int
    cell: int
    width: int
    height: int

    @property
    def origin(self) -> tuple[int, int]:
        """The thumbnail's top-left pixel on its sheet."""
        row, column = divmod(self.cell, CELLS_PER_ROW)
        return (
            column * CELL_SIZE + (CELL_SIZE - self.width) // 2,
            row * CELL_SIZE + (CELL_SIZE - self.height) // 2,
        )


@dataclass(frozen=True)
class Manifest:
    version: int
    sheets: int
    cells: list[Cell]


@dataclass(frozen=True)
class Built:
    version: int | None
    """None when there's no layout, and every set was removed."""
    items: int
    sheets: int
    build_s: float


def hilbert_index(x: NDArray[np.int64], y: NDArray[np.int64], order: int) -> NDArray[np.int64]:
    """Each (x, y)'s distance along the Hilbert curve that fills a 2^order square."""
    n = 1 << order
    x, y = x.copy(), y.copy()
    d = np.zeros_like(x)
    s = n >> 1
    while s > 0:
        rx = (x & s) > 0
        ry = (y & s) > 0
        d += s * s * ((3 * rx.astype(np.int64)) ^ ry.astype(np.int64))
        # Rotate the quadrant, so the curve's next level joins up.
        flip = ~ry & rx
        x[flip] = n - 1 - x[flip]
        y[flip] = n - 1 - y[flip]
        swap = ~ry
        x[swap], y[swap] = y[swap], x[swap]
        s >>= 1
    return d


def curve_order(points: NDArray[np.float64]) -> NDArray[np.intp]:
    """The indices of `points` (in [0, 1]) in the order the Hilbert curve visits them."""
    if len(points) == 0:
        return np.empty(0, dtype=np.intp)
    top = (1 << HILBERT_ORDER) - 1
    grid = np.clip(np.rint(points * top), 0, top).astype(np.int64)
    return np.argsort(hilbert_index(grid[:, 0], grid[:, 1], HILBERT_ORDER), kind="stable")


def encode_manifest(manifest: Manifest) -> bytes:
    cells = sorted(manifest.cells, key=lambda c: c.item_id)
    count = len(cells)
    header = HEADER.pack(
        MAGIC, FORMAT_VERSION, CELL_SIZE, manifest.version, count, manifest.sheets, SHEET_SIZE
    )
    columns = [
        np.fromiter((c.item_id for c in cells), dtype="<u4", count=count),
        np.fromiter((c.sheet for c in cells), dtype="<u2", count=count),
        np.fromiter((c.cell for c in cells), dtype="<u2", count=count),
        np.fromiter((c.width for c in cells), dtype="u1", count=count),
        np.fromiter((c.height for c in cells), dtype="u1", count=count),
    ]
    return b"".join([header, *(column.tobytes() for column in columns)])


def decode_manifest(data: bytes) -> Manifest:
    magic, form, cell_size, version, count, sheets, sheet_size = HEADER.unpack_from(data)
    if (magic, form, cell_size, sheet_size) != (MAGIC, FORMAT_VERSION, CELL_SIZE, SHEET_SIZE):
        raise ValueError("Not an atlas manifest this code reads")
    if len(data) != HEADER.size + 10 * count:
        raise ValueError(f"Wrong length for {count} cells")

    def column(dtype: str, offset: int) -> list[int]:
        return [int(v) for v in np.frombuffer(data, dtype=dtype, count=count, offset=offset)]

    base = HEADER.size
    ids = column("<u4", base)
    sheet = column("<u2", base + 4 * count)
    cell = column("<u2", base + 6 * count)
    width = column("u1", base + 8 * count)
    height = column("u1", base + 9 * count)
    return Manifest(
        version=version,
        sheets=sheets,
        cells=[Cell(*fields) for fields in zip(ids, sheet, cell, width, height, strict=True)],
    )


def build(sessions: sessionmaker[Session], derived_dir: Path) -> Built:
    """Draw the current layout's set of sheets, replacing the others."""
    started_s = time.monotonic()
    with sessions() as session:
        version = session.scalar(latest_version())
        rows: list[tuple[int, float, float]] = (
            [(item_id, x, y) for item_id, x, y in session.execute(_laid_out(version))]
            if version is not None
            else []
        )
    root = atlas_root(derived_dir)
    if version is None:
        _remove_sets(derived_dir, keep=None)
        logger.info("No layout, so no atlases")
        return Built(None, 0, 0, time.monotonic() - started_s)

    thumbnails = {
        item_id: path
        for item_id, _, _ in rows
        if (path := thumbnail_path(derived_dir, item_id, Thumbnail.MAP)).is_file()
    }
    placed = [(item_id, x, y) for item_id, x, y in rows if item_id in thumbnails]
    order = curve_order(np.array([(x, y) for _, x, y in placed], dtype=np.float64).reshape(-1, 2))

    root.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(dir=root, prefix=_STAGING_PREFIX))
    try:
        cells = _draw(staged, [placed[i][0] for i in order], thumbnails)
        sheets = -(-len(order) // CELLS_PER_SHEET)
        manifest = Manifest(version=version, sheets=sheets, cells=cells)
        (staged / MANIFEST).write_bytes(encode_manifest(manifest))
        replace_derived(derived_dir, staged, set_path(derived_dir, version))
    finally:
        remove_derived(derived_dir, staged)  # already gone once swapped in
    _remove_sets(derived_dir, keep=version)
    build_s = time.monotonic() - started_s
    logger.info(
        "Packed %d thumbnails into %d atlas sheet%s for layout version %d in %.1f s",
        len(cells),
        sheets,
        "" if sheets == 1 else "s",
        version,
        build_s,
    )
    return Built(version, len(cells), sheets, build_s)


def _laid_out(version: int) -> sa.Select[int, float, float]:
    return (
        sa.select(Layout.item_id, Layout.x, Layout.y)
        .join(Item, Item.id == Layout.item_id)
        .where(Layout.layout_version == version, Item.status == ItemStatus.ACTIVE)
        .order_by(Layout.item_id)
    )


def _draw(folder: Path, item_ids: list[int], thumbnails: dict[int, Path]) -> list[Cell]:
    """Draw `item_ids`' thumbnails, in order, onto sheets in `folder`."""
    cells: list[Cell] = []
    for sheet, start in enumerate(range(0, len(item_ids), CELLS_PER_SHEET)):
        canvas = Image.new("RGB", (SHEET_SIZE, SHEET_SIZE))
        for cell, item_id in enumerate(item_ids[start : start + CELLS_PER_SHEET]):
            with Image.open(thumbnails[item_id]) as thumbnail:
                picture = thumbnail.convert("RGB")
            placed = Cell(item_id, sheet, cell, picture.width, picture.height)
            canvas.paste(picture, placed.origin)
            cells.append(placed)
        canvas.save(folder / f"{sheet}.jpg", "JPEG", quality=_JPEG_QUALITY, subsampling=0)
    return cells


def _remove_sets(derived_dir: Path, keep: int | None) -> None:
    """Remove every set but `keep`'s, and any staging folder a crash left."""
    root = atlas_root(derived_dir)
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if keep is None or entry.name != str(keep):
            remove_derived(derived_dir, entry)
