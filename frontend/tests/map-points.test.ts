import { describe, expect, it, vi } from "vitest";

import {
  decodePoints,
  fetchPoints,
  fitView,
  FIT_MARGIN,
  idsWithin,
  NO_DAY,
  PointsFormatError,
  POINTS_URL,
  viewBounds,
} from "../src/lib/map-points";

interface Point {
  id: number;
  x: number;
  y: number;
  day: number;
  kind: number;
}

/** The array as `photo_triage.api.map_points.encode` writes it. */
function encode(version: number, points: Point[], format = 1): ArrayBuffer {
  const n = points.length;
  const buffer = new ArrayBuffer(16 + 17 * n);
  const view = new DataView(buffer);
  new Uint8Array(buffer).set(new TextEncoder().encode("PTMP"));
  view.setUint16(4, format, true);
  view.setUint32(8, version, true);
  view.setUint32(12, n, true);
  points.forEach((p, i) => {
    view.setUint32(16 + 4 * i, p.id, true);
    view.setFloat32(16 + 4 * n + 4 * i, p.x, true);
    view.setFloat32(16 + 8 * n + 4 * i, p.y, true);
    view.setInt32(16 + 12 * n + 4 * i, p.day, true);
    view.setUint8(16 + 16 * n + i, p.kind);
  });
  return buffer;
}

const POINTS: Point[] = [
  { id: 3, x: 0, y: 0.25, day: 17_971, kind: 0 },
  { id: 7, x: 1, y: 0.5, day: NO_DAY, kind: 1 },
  { id: 12, x: 0.5, y: 0.75, day: -1, kind: 0 },
];

describe("decodePoints", () => {
  it("reads the header and every column", () => {
    const points = decodePoints(encode(4, POINTS));

    expect(points.version).toBe(4);
    expect(points.count).toBe(3);
    expect([...points.ids]).toEqual([3, 7, 12]);
    expect([...points.positions]).toEqual([0, 0.25, 1, 0.5, 0.5, 0.75]);
    expect([...points.days]).toEqual([17_971, NO_DAY, -1]);
    expect([...points.kinds]).toEqual([0, 1, 0]);
  });

  it("reads an empty layout", () => {
    const points = decodePoints(encode(0, []));
    expect([points.version, points.count, points.positions.length]).toEqual([0, 0, 0]);
  });

  it.each([
    ["too short", new ArrayBuffer(8)],
    ["another format", encode(1, POINTS, 2)],
    ["the wrong length", encode(1, POINTS).slice(0, -1)],
    ["not a points array", new TextEncoder().encode("<html>not this at all</html>").buffer],
  ])("rejects %s", (_, buffer) => {
    expect(() => decodePoints(buffer)).toThrow(PointsFormatError);
  });
});

describe("fetchPoints", () => {
  it("gets the points and decodes them", async () => {
    const fetchFn = vi.fn<typeof fetch>(() =>
      Promise.resolve(new Response(encode(2, POINTS), { status: 200 })),
    );

    const points = await fetchPoints(fetchFn);

    expect(fetchFn).toHaveBeenCalledWith(POINTS_URL, expect.anything());
    expect(points.version).toBe(2);
  });

  it("throws when the response isn't OK", async () => {
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(new Response("", { status: 500 })));
    await expect(fetchPoints(fetchFn)).rejects.toThrow("HTTP 500");
  });
});

describe("fitView", () => {
  const positions = (...xy: number[]) => new Float32Array(xy);

  it("centres the points and fits the longer side, with a margin", () => {
    // 1 wide, 0.5 high, on a square screen: the width decides.
    const view = fitView(positions(0, 0, 1, 0.5), 1000, 1000);

    expect(view.target).toEqual([0.5, 0.25]);
    expect(2 ** view.zoom).toBeCloseTo(1000 * (1 - 2 * FIT_MARGIN));
  });

  it("fits the height on a wide screen", () => {
    const view = fitView(positions(0, 0, 1, 1), 2000, 500);
    expect(2 ** view.zoom).toBeCloseTo(500 * (1 - 2 * FIT_MARGIN));
  });

  it("fits points in a line", () => {
    const view = fitView(positions(0, 0.5, 1, 0.5), 800, 600);
    expect(view.target).toEqual([0.5, 0.5]);
    expect(2 ** view.zoom).toBeCloseTo(800 * (1 - 2 * FIT_MARGIN));
  });

  it.each([
    ["no points", positions()],
    ["one point", positions(0.2, 0.3)],
  ])("shows the whole map for %s", (_, given) => {
    const view = fitView(given, 1000, 1000);
    expect(2 ** view.zoom).toBeCloseTo(1000 * (1 - 2 * FIT_MARGIN));
  });

  it("puts every point on screen", () => {
    const given = positions(0.1, 0.2, 0.9, 0.3, 0.4, 0.8);
    const points = decodePoints(
      encode(
        1,
        [0, 1, 2].map((i) => ({
          id: i,
          x: given[2 * i] ?? 0,
          y: given[2 * i + 1] ?? 0,
          day: 0,
          kind: 0,
        })),
      ),
    );

    const view = fitView(points.positions, 390, 700);

    expect(idsWithin(points, viewBounds(view, 390, 700))).toEqual([0, 1, 2]);
  });
});

describe("idsWithin", () => {
  it("lists the points in the box", () => {
    const points = decodePoints(encode(1, POINTS));
    expect(idsWithin(points, { minX: 0.4, maxX: 1, minY: 0, maxY: 1 })).toEqual([7, 12]);
  });
});

describe("viewBounds", () => {
  it("spans the screen around the target", () => {
    expect(viewBounds({ target: [0.5, 0.5], zoom: 10 }, 1024, 512)).toEqual({
      minX: 0,
      maxX: 1,
      minY: 0.25,
      maxY: 0.75,
    });
  });
});
