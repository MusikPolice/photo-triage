import { describe, expect, it, vi } from "vitest";

import { MediaKind, type MapPoints } from "../src/lib/map-points";
import {
  DIMMED_ALPHA,
  highlight,
  matchCount,
  SearchError,
  searchItems,
  shade,
  thumbnailTint,
  type SearchResults,
} from "../src/lib/search";

function points(ids: number[]): MapPoints {
  return {
    version: 1,
    count: ids.length,
    ids: Uint32Array.from(ids),
    positions: new Float32Array(ids.length * 2),
    days: new Int32Array(ids.length),
    kinds: new Uint8Array(ids.length).fill(MediaKind.photo),
  };
}

function results(ids: number[]): SearchResults {
  return {
    query: "cats",
    floor: 0.24,
    matches: ids.map((id, n) => ({ id, score: 0.3 - n / 100 })),
  };
}

const RGB = Uint8Array.from([10, 20, 30, 40, 50, 60, 70, 80, 90]);

describe("highlight", () => {
  it("marks the matches on the map, best first", () => {
    const shown = highlight(points([7, 3, 9]), results([9, 7]));

    expect([...shown.mask]).toEqual([1, 0, 1]);
    expect(shown.indices).toEqual([2, 0]);
    expect(shown.ids).toEqual([9, 7]);
  });

  it("leaves out matches that aren't on the map", () => {
    const shown = highlight(points([1, 2]), results([5, 2]));

    expect(shown.ids).toEqual([2]);
    expect([...shown.mask]).toEqual([0, 1]);
  });

  it("is empty when nothing matches", () => {
    const shown = highlight(points([1, 2]), results([]));

    expect(shown.ids).toEqual([]);
    expect([...shown.mask]).toEqual([0, 0]);
  });
});

describe("shade", () => {
  it("keeps every point opaque without a search", () => {
    expect([...shade(RGB, null)]).toEqual([10, 20, 30, 255, 40, 50, 60, 255, 70, 80, 90, 255]);
  });

  it("dims what doesn't match, keeping its colour", () => {
    const mask = Uint8Array.from([0, 1, 0]);

    expect([...shade(RGB, mask)]).toEqual([
      10,
      20,
      30,
      DIMMED_ALPHA,
      40,
      50,
      60,
      255,
      70,
      80,
      90,
      DIMMED_ALPHA,
    ]);
  });

  it("restores the map when the search is cleared", () => {
    const searched = shade(RGB, Uint8Array.from([1, 0, 0]));
    const cleared = shade(RGB, null);

    expect([...searched]).not.toEqual([...cleared]);
    expect([...cleared]).toEqual([...shade(RGB, null)]);
  });
});

describe("thumbnailTint", () => {
  it("dims a thumbnail that doesn't match, and no other", () => {
    const mask = Uint8Array.from([1, 0]);

    expect(thumbnailTint(mask, 0)).toEqual([255, 255, 255, 255]);
    expect(thumbnailTint(mask, 1)).toEqual([255, 255, 255, DIMMED_ALPHA]);
    expect(thumbnailTint(null, 1)).toEqual([255, 255, 255, 255]);
  });
});

describe("matchCount", () => {
  it.each([
    [0, "No matches"],
    [1, "1 match"],
    [2, "2 matches"],
    [12345, "12,345 matches"],
  ])("%i", (count, text) => {
    expect(matchCount(count, "en-CA")).toBe(text);
  });
});

describe("searchItems", () => {
  it("asks the API with the query encoded", async () => {
    const body = results([1]);
    const fetchFn = vi.fn(() => Promise.resolve(new Response(JSON.stringify(body))));

    await expect(searchItems("cats & dogs", fetchFn)).resolves.toEqual(body);
    expect(fetchFn).toHaveBeenCalledWith("/api/search?q=cats%20%26%20dogs", expect.anything());
  });

  it("fails with the API's reason", async () => {
    const fetchFn = vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "No exported CLIP model at /models" }), {
          status: 503,
        }),
      ),
    );

    await expect(searchItems("cats", fetchFn)).rejects.toThrow(
      new SearchError("No exported CLIP model at /models"),
    );
  });

  it("fails with the status when there's no reason", async () => {
    const fetchFn = vi.fn(() => Promise.resolve(new Response("oops", { status: 500 })));

    await expect(searchItems("cats", fetchFn)).rejects.toThrow("Search failed with HTTP 500");
  });
});
