import { describe, expect, it, vi } from "vitest";

import {
  changeTag,
  insidePolygon,
  lassoed,
  MAX_ITEMS_PER_REQUEST,
  onMap,
  outlinePath,
  selectedIndices,
  restoreAll,
  selectionAfter,
  tagProblem,
  tagSummary,
  toMap,
  trashAll,
  trashSummary,
  type Vertex,
} from "../src/lib/lasso";
import { shown } from "../src/lib/date-filter";
import type { MapPoints, MapView } from "../src/lib/map-points";

/** Points at the given map positions, with ids 1, 2, 3… */
function pointsAt(...xy: Vertex[]): MapPoints {
  const count = xy.length;
  return {
    version: 1,
    count,
    ids: Uint32Array.from(xy, (_, i) => i + 1),
    positions: Float32Array.from(xy.flat()),
    days: new Int32Array(count),
    kinds: new Uint8Array(count),
  };
}

/** A 100 × 100 pixel map showing map units [0, 1] across: 100 pixels per unit. */
const VIEW: MapView = { target: [0.5, 0.5], zoom: Math.log2(100) };
const SIZE = 100;

/** An outline in pixels, from map units, on that view. */
function outline(...xy: Vertex[]): Vertex[] {
  return xy.map(([x, y]) => [x * SIZE, y * SIZE]);
}

/** A "U": the two arms and the base, round a notch from (0.4, 0) down to (0.6, 0.6). */
const U = [
  [0.1, 0.1],
  [0.4, 0.1],
  [0.4, 0.6],
  [0.6, 0.6],
  [0.6, 0.1],
  [0.9, 0.1],
  [0.9, 0.9],
  [0.1, 0.9],
] as const satisfies Vertex[];

describe("insidePolygon", () => {
  const square: Vertex[] = [
    [0, 0],
    [1, 0],
    [1, 1],
    [0, 1],
  ];

  it("is inside a square, and not beside it", () => {
    expect(insidePolygon(0.5, 0.5, square)).toBe(true);
    expect(insidePolygon(1.5, 0.5, square)).toBe(false);
    expect(insidePolygon(0.5, -0.1, square)).toBe(false);
  });

  it("leaves out the notch of a concave outline", () => {
    expect(insidePolygon(0.5, 0.3, U)).toBe(false);
    expect(insidePolygon(0.2, 0.3, U)).toBe(true);
    expect(insidePolygon(0.8, 0.3, U)).toBe(true);
    expect(insidePolygon(0.5, 0.8, U)).toBe(true);
  });

  it("holds nothing with fewer than 3 vertices", () => {
    expect(insidePolygon(0, 0, [])).toBe(false);
    expect(
      insidePolygon(0.5, 0, [
        [0, 0],
        [1, 0],
      ]),
    ).toBe(false);
  });
});

describe("toMap", () => {
  it("puts the map's centre pixel at the view's target", () => {
    expect(toMap([50, 50], VIEW, SIZE, SIZE)).toEqual([0.5, 0.5]);
  });

  it("goes right and down with x and y, at 2 ** zoom pixels per unit", () => {
    const [x, y] = toMap([75, 10], VIEW, SIZE, SIZE);
    expect(x).toBeCloseTo(0.75);
    expect(y).toBeCloseTo(0.1);
  });
});

describe("lassoed", () => {
  // 1 in the left arm, 2 in the notch, 3 in the base, 4 outside, 5 in the right arm.
  const points = pointsAt([0.2, 0.3], [0.5, 0.3], [0.5, 0.8], [0.95, 0.5], [0.8, 0.3]);

  it("selects the points inside a concave outline, in id order", () => {
    expect(lassoed(points, outline(...U), VIEW, SIZE, SIZE, null)).toEqual([1, 3, 5]);
  });

  it("selects none with an outline of fewer than 3 points", () => {
    expect(lassoed(points, outline([0, 0], [1, 1]), VIEW, SIZE, SIZE, null)).toEqual([]);
  });

  it("follows the view: zoomed in on the left half, the same pixels hold less", () => {
    const zoomed: MapView = { target: [0.25, 0.5], zoom: Math.log2(200) };
    // The whole screen, which now shows x from 0 to 0.5.
    const screen = outline([0, 0], [1, 0], [1, 1], [0, 1]);
    expect(lassoed(points, screen, zoomed, SIZE, SIZE, null)).toEqual([1]);
  });

  it("selects only what a search highlights", () => {
    const mask = Uint8Array.from([0, 0, 1, 1, 1]);
    expect(lassoed(points, outline(...U), VIEW, SIZE, SIZE, mask)).toEqual([3, 5]);
  });

  it("selects only what a date range leaves highlighted", () => {
    const dated = { ...points, days: Int32Array.from([100, 100, 200, 200, 300]) };
    const highlighted = shown(dated, null, Uint8Array.from([1, 1, 0, 0, 1]));
    expect(lassoed(dated, outline(...U), VIEW, SIZE, SIZE, highlighted?.mask ?? null)).toEqual([
      1, 5,
    ]);
  });
});

describe("selectionAfter", () => {
  it("replaces the selection", () => {
    expect(selectionAfter(new Set([1, 2]), [3], false)).toEqual(new Set([3]));
  });

  it("adds to it with Shift", () => {
    expect(selectionAfter(new Set([1, 2]), [2, 3], true)).toEqual(new Set([1, 2, 3]));
  });
});

describe("onMap", () => {
  const points = pointsAt([0, 0], [1, 1]);

  it("keeps the same set when every item is still there", () => {
    const selection = new Set([1, 2]);
    expect(onMap(selection, points)).toBe(selection);
  });

  it("drops the items no longer on the map", () => {
    expect(onMap(new Set([1, 7]), points)).toEqual(new Set([1]));
  });
});

describe("selectedIndices", () => {
  it("gives the selected items' point indices", () => {
    expect(selectedIndices(pointsAt([0, 0], [1, 1], [2, 2]), new Set([3, 1]))).toEqual([0, 2]);
  });
});

describe("outlinePath", () => {
  it("is a closed path", () => {
    expect(
      outlinePath([
        [0, 0],
        [10, 0],
        [10, 5],
      ]),
    ).toBe("M0.0,0.0L10.0,0.0L10.0,5.0Z");
    expect(outlinePath([])).toBe("");
  });
});

function answering(body: (sent: { item_ids: number[] }) => unknown) {
  return vi.fn((_url: string, init?: RequestInit) => {
    const sent = JSON.parse(init?.body as string) as { item_ids: number[] };
    return Promise.resolve(new Response(JSON.stringify(body(sent))));
  });
}

describe("trashAll", () => {
  it("sends a request per MAX_ITEMS_PER_REQUEST items, and adds up the results", async () => {
    const ids = Array.from({ length: MAX_ITEMS_PER_REQUEST + 2 }, (_, i) => i + 1);
    const fetchFn = answering((sent) => ({ moved: sent.item_ids, skipped: [] }));
    const result = await trashAll(ids, fetchFn as unknown as typeof fetch);
    expect(fetchFn).toHaveBeenCalledTimes(2);
    expect(fetchFn.mock.calls[0]?.[0]).toBe("/api/trash");
    expect(result.moved).toEqual(ids);
  });
});

describe("changeTag", () => {
  it("posts the tag and action for the items", async () => {
    const fetchFn = answering((sent) => ({ changed: sent.item_ids, unchanged: [], skipped: [] }));
    const result = await changeTag([4, 5], "beach", "add", fetchFn as unknown as typeof fetch);
    expect(fetchFn.mock.calls[0]?.[0]).toBe("/api/tags");
    expect(JSON.parse(fetchFn.mock.calls[0]?.[1]?.body as string)).toEqual({
      item_ids: [4, 5],
      tag: "beach",
      action: "add",
    });
    expect(result.changed).toEqual([4, 5]);
  });
});

describe("tagSummary", () => {
  it("says what was tagged, and what already had it", () => {
    expect(tagSummary("add", "beach", { changed: [1, 2], unchanged: [3], skipped: [] })).toBe(
      "Tagged 2 items “beach”. 1 item already had it.",
    );
  });

  it("says what a tag was removed from, and why any were skipped", () => {
    expect(
      tagSummary("remove", "beach", {
        changed: [1],
        unchanged: [],
        skipped: [{ id: 9, reason: "not in the library" }],
      }),
    ).toBe("Removed “beach” from 1 item. 1 item couldn't be: not in the library.");
  });
});

describe("restoreAll", () => {
  it("posts to the restore route", async () => {
    const fetchFn = answering((sent) => ({ moved: sent.item_ids, skipped: [] }));
    await restoreAll([1], fetchFn as unknown as typeof fetch);
    expect(fetchFn.mock.calls[0]?.[0]).toBe("/api/trash/restore");
  });
});

describe("trashSummary", () => {
  it("says how many went to the trash", () => {
    expect(trashSummary({ moved: [1, 2], skipped: [] })).toBe("Moved 2 items to the trash.");
    expect(trashSummary({ moved: [1], skipped: [] })).toBe("Moved 1 item to the trash.");
  });

  it("says why any weren't", () => {
    expect(trashSummary({ moved: [], skipped: [{ id: 1, reason: "already in the trash" }] })).toBe(
      "1 item couldn't be: already in the trash.",
    );
  });
});

describe("tagProblem", () => {
  it.each([
    ["beach", null],
    ["  beach  ", null],
    ["", "Type a tag first."],
    ["   ", "Type a tag first."],
    ["a,b", "A tag can't contain a comma."],
    ["x".repeat(101), "A tag can't be longer than 100 characters."],
  ])("%j is %j", (tag, problem) => {
    expect(tagProblem(tag)).toBe(problem);
  });
});
