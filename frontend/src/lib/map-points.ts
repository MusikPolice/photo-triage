/** The map's points, from `GET /api/map/points` (plan §6.4), and the view that fits
 * them. The binary format is documented in the backend's `photo_triage.api.map_points`:
 * a 16-byte header, then one little-endian column per field. */

export const POINTS_URL = "/api/map/points";
export const MAGIC = "PTMP";
export const FORMAT_VERSION = 1;
const HEADER_BYTES = 16;
const BYTES_PER_POINT = 17;

/** The capture day of an item with no capture time. */
export const NO_DAY = -(2 ** 31);

export const MediaKind = { photo: 0, video: 1 } as const;

export interface MapPoints {
  /** The layout version, or 0 before the first layout. */
  version: number;
  count: number;
  ids: Uint32Array;
  /** x and y of each point in turn, for deck.gl's `getPosition`. In [0, 1]. */
  positions: Float32Array;
  /** Days from 1970-01-01 to each capture date, or `NO_DAY`. */
  days: Int32Array;
  /** `MediaKind` of each point. */
  kinds: Uint8Array;
}

export class PointsFormatError extends Error {}

/** Read the binary array. Throws `PointsFormatError` if it isn't one this code knows. */
export function decodePoints(buffer: ArrayBuffer): MapPoints {
  if (buffer.byteLength < HEADER_BYTES) throw new PointsFormatError("Too short for a header");
  const header = new DataView(buffer, 0, HEADER_BYTES);
  const magic = String.fromCharCode(...new Uint8Array(buffer, 0, 4));
  if (magic !== MAGIC) throw new PointsFormatError(`Not a points array: ${magic}`);
  const format = header.getUint16(4, true);
  if (format !== FORMAT_VERSION) throw new PointsFormatError(`Unknown format ${String(format)}`);
  const version = header.getUint32(8, true);
  const count = header.getUint32(12, true);
  if (buffer.byteLength !== HEADER_BYTES + BYTES_PER_POINT * count) {
    throw new PointsFormatError(`Wrong length for ${String(count)} points`);
  }
  // Typed arrays read in the platform's byte order, which is little-endian on every
  // browser this runs on.
  const column = (index: number) => HEADER_BYTES + 4 * index * count;
  const xs = new Float32Array(buffer, column(1), count);
  const ys = new Float32Array(buffer, column(2), count);
  const positions = new Float32Array(2 * count);
  for (let i = 0; i < count; i++) {
    positions[2 * i] = xs[i] ?? 0;
    positions[2 * i + 1] = ys[i] ?? 0;
  }
  return {
    version,
    count,
    ids: new Uint32Array(buffer, column(0), count),
    positions,
    days: new Int32Array(buffer, column(3), count),
    kinds: new Uint8Array(buffer, column(4), count),
  };
}

export async function fetchPoints(fetchFn: typeof fetch = fetch): Promise<MapPoints> {
  const response = await fetchFn(POINTS_URL, { headers: { Accept: "application/octet-stream" } });
  if (!response.ok)
    throw new Error(`GET ${POINTS_URL} failed with HTTP ${String(response.status)}`);
  return decodePoints(await response.arrayBuffer());
}

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** The smallest box holding every point, or null if there are none. */
export function pointBounds(positions: Float32Array): Bounds | null {
  if (positions.length < 2) return null;
  const box = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  for (let i = 0; i < positions.length; i += 2) {
    const x = positions[i] ?? 0;
    const y = positions[i + 1] ?? 0;
    box.minX = Math.min(box.minX, x);
    box.maxX = Math.max(box.maxX, x);
    box.minY = Math.min(box.minY, y);
    box.maxY = Math.max(box.maxY, y);
  }
  return box;
}

/** A deck.gl `OrthographicView` view state: 2 ** zoom pixels per map unit. */
export interface MapView {
  target: [number, number];
  zoom: number;
}

/** Margin left around the points, as a fraction of each side of the screen. */
export const FIT_MARGIN = 0.05;

/** The view that shows every point on a `width` × `height` pixel screen, centred,
 * with `FIT_MARGIN` around them. One point, or none, gets the whole map's [0, 1]
 * square. */
export function fitView(positions: Float32Array, width: number, height: number): MapView {
  const box = pointBounds(positions) ?? { minX: 0, minY: 0, maxX: 1, maxY: 1 };
  const spanX = box.maxX - box.minX;
  const spanY = box.maxY - box.minY;
  const span = spanX > 0 || spanY > 0 ? { x: spanX, y: spanY } : { x: 1, y: 1 };
  const usable = 1 - 2 * FIT_MARGIN;
  const scale = Math.min(
    span.x > 0 ? (width * usable) / span.x : Infinity,
    span.y > 0 ? (height * usable) / span.y : Infinity,
  );
  return {
    target: [(box.minX + box.maxX) / 2, (box.minY + box.maxY) / 2],
    zoom: Math.log2(Math.max(scale, Number.MIN_VALUE)),
  };
}

/** The map area a view shows on a `width` × `height` screen. */
export function viewBounds(view: MapView, width: number, height: number): Bounds {
  const scale = 2 ** view.zoom;
  const [x, y] = view.target;
  const halfX = width / 2 / scale;
  const halfY = height / 2 / scale;
  return { minX: x - halfX, maxX: x + halfX, minY: y - halfY, maxY: y + halfY };
}

/** The indices of the points inside `bounds`, in order. */
export function indicesWithin(points: MapPoints, bounds: Bounds): number[] {
  const inside: number[] = [];
  for (let i = 0; i < points.count; i++) {
    const x = points.positions[2 * i] ?? NaN;
    const y = points.positions[2 * i + 1] ?? NaN;
    if (x >= bounds.minX && x <= bounds.maxX && y >= bounds.minY && y <= bounds.maxY) {
      inside.push(i);
    }
  }
  return inside;
}

/** The ids of the points inside `bounds`, in id order. */
export function idsWithin(points: MapPoints, bounds: Bounds): number[] {
  return indicesWithin(points, bounds).map((i) => points.ids[i] ?? 0);
}

/** `bounds` grown by `fraction` of its width and height on every side. */
export function grown(bounds: Bounds, fraction: number): Bounds {
  const dx = (bounds.maxX - bounds.minX) * fraction;
  const dy = (bounds.maxY - bounds.minY) * fraction;
  return {
    minX: bounds.minX - dx,
    maxX: bounds.maxX + dx,
    minY: bounds.minY - dy,
    maxY: bounds.maxY + dy,
  };
}

/** `points` without the items in `hidden`: those moved to the trash since the
 * points were loaded. The same object if none of them is there. */
export function withoutIds(points: MapPoints, hidden: ReadonlySet<number>): MapPoints {
  if (hidden.size === 0) return points;
  const kept: number[] = [];
  for (let i = 0; i < points.count; i++) {
    if (!hidden.has(points.ids[i] ?? 0)) kept.push(i);
  }
  if (kept.length === points.count) return points;
  const count = kept.length;
  const out: MapPoints = {
    version: points.version,
    count,
    ids: new Uint32Array(count),
    positions: new Float32Array(2 * count),
    days: new Int32Array(count),
    kinds: new Uint8Array(count),
  };
  kept.forEach((from, to) => {
    out.ids[to] = points.ids[from] ?? 0;
    out.positions[2 * to] = points.positions[2 * from] ?? 0;
    out.positions[2 * to + 1] = points.positions[2 * from + 1] ?? 0;
    out.days[to] = points.days[from] ?? 0;
    out.kinds[to] = points.kinds[from] ?? 0;
  });
  return out;
}
