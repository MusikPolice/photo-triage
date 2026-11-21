import { describe, expect, it, vi } from "vitest";

import {
  ATLAS_URL,
  AtlasFormatError,
  atlasIndex,
  capacity,
  cellRect,
  decodeAtlas,
  fetchAtlas,
  HYSTERESIS,
  iconRect,
  nextDetail,
  sheetGroups,
  sheetsFor,
  sheetUrl,
  THUMBNAIL_PX,
  thumbnailSize,
} from "../src/lib/map-atlas";
import { grown, indicesWithin, MediaKind, type MapPoints } from "../src/lib/map-points";

const CELL = 64;
const SHEET = 4096;
const PER_ROW = SHEET / CELL;

interface Entry {
  id: number;
  sheet: number;
  cell: number;
  width: number;
  height: number;
}

/** A manifest as `photo_triage.pipeline.atlases.encode_manifest` writes it. */
function manifest(version: number, sheets: number, entries: Entry[]): ArrayBuffer {
  const n = entries.length;
  const buffer = new ArrayBuffer(20 + 10 * n);
  const view = new DataView(buffer);
  new Uint8Array(buffer).set(new TextEncoder().encode("PTAT"));
  view.setUint16(4, 1, true);
  view.setUint16(6, CELL, true);
  view.setUint32(8, version, true);
  view.setUint32(12, n, true);
  view.setUint16(16, sheets, true);
  view.setUint16(18, SHEET, true);
  entries.forEach((e, i) => {
    view.setUint32(20 + 4 * i, e.id, true);
    view.setUint16(20 + 4 * n + 2 * i, e.sheet, true);
    view.setUint16(20 + 6 * n + 2 * i, e.cell, true);
    view.setUint8(20 + 8 * n + i, e.width);
    view.setUint8(20 + 9 * n + i, e.height);
  });
  return buffer;
}

function points(ids: number[], xy: [number, number][] = ids.map(() => [0, 0])): MapPoints {
  return {
    version: 4,
    count: ids.length,
    ids: Uint32Array.from(ids),
    positions: Float32Array.from(xy.flat()),
    days: new Int32Array(ids.length),
    kinds: new Uint8Array(ids.length).fill(MediaKind.photo),
  };
}

const ENTRIES: Entry[] = [
  { id: 3, sheet: 0, cell: 0, width: 64, height: 48 },
  { id: 7, sheet: 0, cell: PER_ROW + 2, width: 36, height: 64 },
  { id: 9, sheet: 1, cell: 5, width: 64, height: 64 },
];

describe("decodeAtlas", () => {
  it("reads the header and every column", () => {
    const atlas = decodeAtlas(manifest(4, 2, ENTRIES));

    expect([atlas.version, atlas.sheets, atlas.count]).toEqual([4, 2, 3]);
    expect([atlas.cellSize, atlas.sheetSize]).toEqual([CELL, SHEET]);
    expect([...atlas.ids]).toEqual([3, 7, 9]);
    expect([...atlas.sheet]).toEqual([0, 0, 1]);
    expect([...atlas.cell]).toEqual([0, PER_ROW + 2, 5]);
    expect([...atlas.width]).toEqual([64, 36, 64]);
    expect([...atlas.height]).toEqual([48, 64, 64]);
  });

  it("reads the empty manifest from before the first set", () => {
    expect(decodeAtlas(manifest(0, 0, []))).toMatchObject({ version: 0, count: 0, sheets: 0 });
  });

  function withMagic(magic: string): ArrayBuffer {
    const buffer = manifest(1, 1, []);
    new Uint8Array(buffer).set(new TextEncoder().encode(magic));
    return buffer;
  }

  it.each([
    ["a short buffer", new ArrayBuffer(8)],
    ["the points' magic", withMagic("PTMP")],
    ["the wrong length", manifest(1, 1, ENTRIES).slice(0, -1)],
  ])("rejects %s", (_, buffer) => {
    expect(() => decodeAtlas(buffer)).toThrow(AtlasFormatError);
  });

  it("rejects an unknown format version", () => {
    const buffer = manifest(1, 1, []);
    new DataView(buffer).setUint16(4, 2, true);
    expect(() => decodeAtlas(buffer)).toThrow(/Unknown format 2/);
  });

  it("is fetched from the API", async () => {
    const fetchFn = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(manifest(4, 2, ENTRIES))),
    );
    expect((await fetchAtlas(fetchFn)).count).toBe(3);
    expect(fetchFn).toHaveBeenCalledWith(ATLAS_URL, expect.anything());
  });

  it("fails on an HTTP error", async () => {
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(new Response("", { status: 503 })));
    await expect(fetchAtlas(fetchFn)).rejects.toThrow("HTTP 503");
  });
});

describe("cellRect", () => {
  const atlas = decodeAtlas(manifest(4, 2, ENTRIES));

  it("centres a landscape thumbnail in the top left cell", () => {
    expect(cellRect(atlas, 0)).toEqual({ x: 0, y: 8, width: 64, height: 48 });
  });

  it("counts cells row by row, and centres a portrait thumbnail", () => {
    // Row 1, column 2; 36 px wide, so 14 px in from the cell's left.
    expect(cellRect(atlas, 1)).toEqual({ x: 2 * CELL + 14, y: CELL, width: 36, height: 64 });
  });

  it("draws each thumbnail less a pixel on each side, clear of its neighbours", () => {
    expect(iconRect(atlas, 0)).toEqual({ x: 1, y: 9, width: 62, height: 46 });
  });

  it("names each sheet's URL by layout version", () => {
    expect(sheetUrl(4, 1)).toBe("/api/map/atlases/4/1.jpg");
  });
});

describe("atlasIndex", () => {
  const atlas = decodeAtlas(manifest(4, 2, ENTRIES));

  it("finds each point's cell, whatever order the points are in", () => {
    expect([...atlasIndex(points([9, 3, 7]), atlas)]).toEqual([2, 0, 1]);
  });

  it("gives -1 to a point with no cell", () => {
    expect([...atlasIndex(points([1, 7, 8, 10]), atlas)]).toEqual([-1, 1, -1, -1]);
  });

  it("gives every point -1 before the first set", () => {
    expect([...atlasIndex(points([3, 7]), decodeAtlas(manifest(0, 0, [])))]).toEqual([-1, -1]);
  });
});

describe("sheets", () => {
  const atlas = decodeAtlas(manifest(4, 2, ENTRIES));
  // Ids 3 and 7 on sheet 0, at the left; 9 on sheet 1, at the right; 5 has no cell.
  const shown = points(
    [3, 5, 7, 9],
    [
      [0.1, 0.5],
      [0.2, 0.5],
      [0.3, 0.5],
      [0.9, 0.5],
    ],
  );
  const index = atlasIndex(shown, atlas);

  it("group the points with a cell by sheet, keyed by point index", () => {
    const groups = sheetGroups(atlas, index);
    expect([...groups.keys()]).toEqual([0, 1]);
    expect(groups.get(0)?.points).toEqual([0, 2]);
    expect(groups.get(0)?.mapping["2"]).toEqual(iconRect(atlas, 1));
    expect(groups.get(1)?.points).toEqual([3]);
  });

  it("needed for a view are just the ones holding the items in it", () => {
    const left = indicesWithin(shown, { minX: 0, maxX: 0.5, minY: 0, maxY: 1 });
    const right = indicesWithin(shown, { minX: 0.5, maxX: 1, minY: 0, maxY: 1 });
    const noCell = indicesWithin(shown, { minX: 0.15, maxX: 0.25, minY: 0, maxY: 1 });

    expect(sheetsFor(left, atlas, index)).toEqual([0]);
    expect(sheetsFor(right, atlas, index)).toEqual([1]);
    expect(sheetsFor([...left, ...right], atlas, index)).toEqual([0, 1]);
    expect(sheetsFor(noCell, atlas, index)).toEqual([]);
  });

  it("grow a view's bounds to load sheets just beyond its edges", () => {
    expect(grown({ minX: 0, maxX: 1, minY: 2, maxY: 4 }, 0.25)).toEqual({
      minX: -0.25,
      maxX: 1.25,
      minY: 1.5,
      maxY: 4.5,
    });
  });
});

describe("the dots-or-thumbnails threshold", () => {
  const room = capacity(1280, 800);

  it("is how many thumbnails fill the screen side by side", () => {
    expect(room).toBe(Math.floor((1280 * 800) / THUMBNAIL_PX ** 2));
  });

  it("shows dots while more items are in view than would fit", () => {
    expect(nextDetail("dots", room + 1, room)).toBe("dots");
  });

  it("switches to thumbnails once they'd fit", () => {
    expect(nextDetail("dots", room, room)).toBe("thumbnails");
  });

  it("goes back to dots only well past the threshold, so it doesn't flicker", () => {
    expect(nextDetail("thumbnails", room + 1, room)).toBe("thumbnails");
    expect(nextDetail("thumbnails", Math.floor(room * HYSTERESIS), room)).toBe("thumbnails");
    expect(nextDetail("thumbnails", Math.floor(room * HYSTERESIS) + 1, room)).toBe("dots");
  });
});

describe("thumbnailSize", () => {
  it.each([
    [{ x: 0, y: 0, width: 64, height: 48 }, THUMBNAIL_PX * 0.75],
    [{ x: 0, y: 0, width: 36, height: 64 }, THUMBNAIL_PX],
    [{ x: 0, y: 0, width: 0, height: 0 }, 0],
  ])("draws %o with its longer side THUMBNAIL_PX", (rect, size) => {
    expect(thumbnailSize(rect)).toBe(size);
  });
});
