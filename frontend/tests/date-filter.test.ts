import { describe, expect, it } from "vitest";

import {
  dateMask,
  formatMonth,
  fractionOf,
  monthAt,
  monthOf,
  monthSpan,
  moveEnd,
  moveNearer,
  narrowed,
  shown,
} from "../src/lib/date-filter";
import { MediaKind, NO_DAY, type MapPoints } from "../src/lib/map-points";
import { highlight } from "../src/lib/search";

const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 86_400_000;
const month = (year: number, january0: number) => (year - 1970) * 12 + january0;

function points(days: number[]): MapPoints {
  return {
    version: 1,
    count: days.length,
    ids: Uint32Array.from(days.map((_, i) => 10 + i)),
    positions: new Float32Array(days.length * 2),
    days: Int32Array.from(days),
    kinds: new Uint8Array(days.length).fill(MediaKind.photo),
  };
}

// ids 10..14: 2012-07, 2019-03, undated, 2024-01, 2024-11
const LIBRARY = points([
  day("2012-07-27"),
  day("2019-03-31"),
  NO_DAY,
  day("2024-01-01"),
  day("2024-11-30"),
]);
const SPAN = { from: month(2012, 6), to: month(2024, 10) };

describe("monthOf", () => {
  it.each([
    ["1970-01-01", 0],
    ["1970-12-31", 11],
    ["2024-11-30", month(2024, 10)],
    ["1969-12-31", -1],
  ])("%s", (iso, expected) => {
    expect(monthOf(day(iso))).toBe(expected);
  });

  it("is null for an undated item", () => {
    expect(monthOf(NO_DAY)).toBeNull();
  });
});

describe("monthSpan", () => {
  it("runs from the first capture month to the last, skipping undated items", () => {
    expect(monthSpan(LIBRARY.days)).toEqual(SPAN);
  });

  it("is null when nothing has a date", () => {
    expect(monthSpan(Int32Array.from([NO_DAY, NO_DAY]))).toBeNull();
    expect(monthSpan(new Int32Array(0))).toBeNull();
  });
});

describe("dateMask", () => {
  it("is no filter while the range is the whole span", () => {
    expect(narrowed(SPAN, SPAN)).toBe(false);
    expect(dateMask(LIBRARY, SPAN, SPAN)).toBeNull();
    expect(dateMask(LIBRARY, null, SPAN)).toBeNull();
  });

  it("keeps the months in range, both ends included", () => {
    const range = { from: month(2019, 2), to: month(2024, 0) };

    expect([...(dateMask(LIBRARY, range, SPAN) ?? [])]).toEqual([0, 1, 0, 1, 0]);
  });

  it("leaves out undated items once the range is narrowed", () => {
    const range = { from: SPAN.from, to: month(2024, 9) };

    expect([...(dateMask(LIBRARY, range, SPAN) ?? [])]).toEqual([1, 1, 0, 1, 0]);
  });
});

describe("shown: the filter and search combined", () => {
  const search = highlight(LIBRARY, {
    query: "cats",
    floor: 0.24,
    matches: [
      { id: 14, score: 0.3 },
      { id: 12, score: 0.28 },
      { id: 11, score: 0.26 },
    ],
  });
  const range = { from: month(2019, 0), to: month(2024, 0) };
  const dates = dateMask(LIBRARY, range, SPAN);

  it("highlights nothing in particular with neither", () => {
    expect(shown(LIBRARY, null, null)).toBeNull();
  });

  it("highlights what's in range with the filter alone, unringed", () => {
    expect(shown(LIBRARY, null, dates)).toEqual({
      mask: Uint8Array.from([0, 1, 0, 1, 0]),
      ids: [11, 13],
      matches: null,
    });
  });

  it("highlights the search's matches with the search alone, best first", () => {
    const result = shown(LIBRARY, search, null);

    expect(result?.ids).toEqual([14, 12, 11]);
    expect(result?.matches).toEqual([4, 2, 1]);
  });

  it("highlights only matches in range with both", () => {
    const result = shown(LIBRARY, search, dates);

    // 14 is after the range, and 12 is undated.
    expect(result?.ids).toEqual([11]);
    expect(result?.matches).toEqual([1]);
    expect([...(result?.mask ?? [])]).toEqual([0, 1, 0, 0, 0]);
  });
});

describe("the slider's positions", () => {
  it("maps a fraction of the track to a month, and back", () => {
    expect(monthAt(0, SPAN)).toBe(SPAN.from);
    expect(monthAt(1, SPAN)).toBe(SPAN.to);
    expect(monthAt(-0.5, SPAN)).toBe(SPAN.from);
    expect(monthAt(1.5, SPAN)).toBe(SPAN.to);
    expect(fractionOf(SPAN.to, SPAN)).toBe(1);
    expect(fractionOf(monthAt(0.5, SPAN), SPAN)).toBeCloseTo(0.5, 2);
    expect(fractionOf(5, { from: 5, to: 5 })).toBe(0);
  });

  it("moves the nearer end to a tap", () => {
    const range = { from: 10, to: 20 };

    expect(moveNearer(range, 12)).toEqual({ from: 12, to: 20 });
    expect(moveNearer(range, 19)).toEqual({ from: 10, to: 19 });
    expect(moveNearer(range, 25)).toEqual({ from: 10, to: 25 });
    expect(moveNearer(range, 2)).toEqual({ from: 2, to: 20 });
    expect(moveNearer({ from: 10, to: 10 }, 12)).toEqual({ from: 10, to: 12 });
    expect(moveNearer({ from: 10, to: 10 }, 8)).toEqual({ from: 8, to: 10 });
  });

  it("keeps one end from passing the other", () => {
    expect(moveEnd({ from: 10, to: 20 }, "from", 25)).toEqual({ from: 20, to: 20 });
    expect(moveEnd({ from: 10, to: 20 }, "to", 5)).toEqual({ from: 10, to: 10 });
  });

  it("formats a month", () => {
    expect(formatMonth(month(2014, 2), "en-CA")).toBe("Mar 2014");
  });
});
