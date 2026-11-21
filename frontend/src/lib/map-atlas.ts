/** Thumbnails on the map (plan §6.4 "Rendering"): past a zoom threshold the dots give
 * way to each item's 64 px thumbnail, cut from the atlas sheets of
 * `GET /api/map/atlases`. The manifest's binary format is documented in the
 * backend's `photo_triage.pipeline.atlases`: a 20-byte header, then one
 * little-endian column per field. */

import type { MapPoints } from "./map-points";

export const ATLAS_URL = "/api/map/atlases";
export const MAGIC = "PTAT";
export const FORMAT_VERSION = 1;
const HEADER_BYTES = 20;
const BYTES_PER_CELL = 10;

export interface Atlas {
  /** The layout version the sheets were drawn for, or 0 before the first set. */
  version: number;
  cellSize: number;
  sheetSize: number;
  sheets: number;
  count: number;
  /** Item ids, ascending. */
  ids: Uint32Array;
  sheet: Uint16Array;
  /** Cell within the sheet, row by row from the top left. */
  cell: Uint16Array;
  width: Uint8Array;
  height: Uint8Array;
}

export class AtlasFormatError extends Error {}

/** Read the manifest. Throws `AtlasFormatError` if it isn't one this code knows. */
export function decodeAtlas(buffer: ArrayBuffer): Atlas {
  if (buffer.byteLength < HEADER_BYTES) throw new AtlasFormatError("Too short for a header");
  const header = new DataView(buffer, 0, HEADER_BYTES);
  const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 4));
  if (magic !== MAGIC) throw new AtlasFormatError(`Not an atlas manifest: ${magic}`);
  const format = header.getUint16(4, true);
  if (format !== FORMAT_VERSION) throw new AtlasFormatError(`Unknown format ${String(format)}`);
  const count = header.getUint32(12, true);
  if (buffer.byteLength !== HEADER_BYTES + BYTES_PER_CELL * count) {
    throw new AtlasFormatError(`Wrong length for ${String(count)} cells`);
  }
  // Typed arrays read in the platform's byte order, little-endian on every browser
  // this runs on. Each column starts at a multiple of its width.
  return {
    cellSize: header.getUint16(6, true),
    version: header.getUint32(8, true),
    count,
    sheets: header.getUint16(16, true),
    sheetSize: header.getUint16(18, true),
    ids: new Uint32Array(buffer, HEADER_BYTES, count),
    sheet: new Uint16Array(buffer, HEADER_BYTES + 4 * count, count),
    cell: new Uint16Array(buffer, HEADER_BYTES + 6 * count, count),
    width: new Uint8Array(buffer, HEADER_BYTES + 8 * count, count),
    height: new Uint8Array(buffer, HEADER_BYTES + 9 * count, count),
  };
}

export async function fetchAtlas(fetchFn: typeof fetch = fetch): Promise<Atlas> {
  const response = await fetchFn(ATLAS_URL, { headers: { Accept: "application/octet-stream" } });
  if (!response.ok) throw new Error(`GET ${ATLAS_URL} failed with HTTP ${String(response.status)}`);
  return decodeAtlas(await response.arrayBuffer());
}

export function sheetUrl(version: number, sheet: number): string {
  return `${ATLAS_URL}/${String(version)}/${String(sheet)}.jpg`;
}

/** Where a thumbnail sits on its sheet, in pixels from the sheet's top left. */
export interface CellRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Pixels trimmed from each edge of a thumbnail when it's cut from its sheet. The
 * GPU blends neighbouring pixels when it scales a texture, so a cut right at the
 * edge picks up a faint line of whatever is next to it on the sheet. */
export const INSET_PX = 1;

/** The part of a sheet to draw for the atlas's `j`th thumbnail: the thumbnail,
 * centred in its cell as the backend draws it, less `INSET_PX` on each side. */
export function iconRect(atlas: Atlas, j: number): CellRect {
  const { x, y, width, height } = cellRect(atlas, j);
  const inset = Math.min(INSET_PX, Math.floor(Math.min(width, height) / 4));
  return { x: x + inset, y: y + inset, width: width - 2 * inset, height: height - 2 * inset };
}

/** The pixels of the atlas's `j`th thumbnail: centred in its cell, as the backend
 * draws it. */
export function cellRect(atlas: Atlas, j: number): CellRect {
  const perRow = Math.floor(atlas.sheetSize / atlas.cellSize);
  const cell = atlas.cell[j] ?? 0;
  const width = atlas.width[j] ?? 0;
  const height = atlas.height[j] ?? 0;
  return {
    x: (cell % perRow) * atlas.cellSize + Math.floor((atlas.cellSize - width) / 2),
    y: Math.floor(cell / perRow) * atlas.cellSize + Math.floor((atlas.cellSize - height) / 2),
    width,
    height,
  };
}

/** For each point, its index in the atlas, or -1 if it has no cell (no thumbnail,
 * or new since the sheets were drawn). */
export function atlasIndex(points: MapPoints, atlas: Atlas): Int32Array {
  const out = new Int32Array(points.count).fill(-1);
  for (let i = 0; i < points.count; i++) {
    const id = points.ids[i] ?? 0;
    let low = 0;
    let high = atlas.count - 1;
    while (low <= high) {
      const mid = (low + high) >>> 1;
      const at = atlas.ids[mid] ?? 0;
      if (at === id) {
        out[i] = mid;
        break;
      }
      if (at < id) low = mid + 1;
      else high = mid - 1;
    }
  }
  return out;
}

/** One sheet's thumbnails, ready for a deck.gl `IconLayer`. */
export interface SheetGroup {
  sheet: number;
  /** Indices into the points. */
  points: number[];
  /** Keyed by the point's index, as a string. */
  mapping: Record<string, CellRect>;
}

/** The points with a cell, grouped by sheet. */
export function sheetGroups(atlas: Atlas, index: Int32Array): Map<number, SheetGroup> {
  const groups = new Map<number, SheetGroup>();
  index.forEach((j, i) => {
    if (j < 0) return;
    const sheet = atlas.sheet[j] ?? 0;
    let group = groups.get(sheet);
    if (group === undefined) {
      group = { sheet, points: [], mapping: {} };
      groups.set(sheet, group);
    }
    group.points.push(i);
    group.mapping[String(i)] = iconRect(atlas, j);
  });
  return groups;
}

/** The sheets holding any of `shown` (indices into the points), ascending. */
export function sheetsFor(shown: Iterable<number>, atlas: Atlas, index: Int32Array): number[] {
  const sheets = new Set<number>();
  for (const i of shown) {
    const j = index[i] ?? -1;
    if (j >= 0) sheets.add(atlas.sheet[j] ?? 0);
  }
  return [...sheets].sort((a, b) => a - b);
}

/** Thumbnails are drawn with their longer side this many pixels. */
export const THUMBNAIL_PX = 48;
/** Once showing thumbnails, the map goes back to dots only with this many times as
 * many items in view, so it doesn't flicker between the two at the threshold. */
export const HYSTERESIS = 1.25;

export type Detail = "dots" | "thumbnails";

/** How many thumbnails would fill a `width` × `height` screen, side by side. */
export function capacity(width: number, height: number): number {
  return Math.floor((width * height) / THUMBNAIL_PX ** 2);
}

/** Dots or thumbnails, with `visible` items in view: thumbnails once they'd all fit
 * on screen side by side. Zooming in only ever lowers `visible`, so this is a zoom
 * threshold that adapts to how crowded that part of the map is. */
export function nextDetail(current: Detail, visible: number, room: number): Detail {
  if (current === "dots") return visible <= room ? "thumbnails" : "dots";
  return visible > room * HYSTERESIS ? "dots" : "thumbnails";
}

/** A thumbnail's size for `IconLayer`'s `getSize`, its height in pixels, so that
 * its longer side is `THUMBNAIL_PX`. */
export function thumbnailSize(rect: CellRect): number {
  const longer = Math.max(rect.width, rect.height);
  return longer === 0 ? 0 : (THUMBNAIL_PX * rect.height) / longer;
}
