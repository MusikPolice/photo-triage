import { describe, expect, it } from "vitest";

import {
  hexToRgb,
  legend,
  MEDIA_COLOURS,
  pointColours,
  rampColour,
  UNKNOWN_COLOUR,
  YEAR_RAMP,
  yearColour,
  yearOf,
  yearRange,
  type Rgb,
  type Theme,
} from "../src/lib/map-colours";
import { MediaKind, NO_DAY, type MapPoints } from "../src/lib/map-points";

const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 86_400_000;
const PLAIN: Rgb = [1, 2, 3];

function points(days: number[], kinds: number[] = days.map(() => MediaKind.photo)): MapPoints {
  return {
    version: 1,
    count: days.length,
    ids: Uint32Array.from(days.map((_, i) => i + 1)),
    positions: new Float32Array(days.length * 2),
    days: Int32Array.from(days),
    kinds: Uint8Array.from(kinds),
  };
}

function colourAt(colours: Uint8Array, i: number): number[] {
  return [...colours.slice(3 * i, 3 * i + 3)];
}

describe("yearOf", () => {
  it.each([
    ["2019-04-15", 2019],
    ["1970-01-01", 1970],
    ["1969-12-31", 1969],
    ["2026-12-31", 2026],
  ])("reads %s as %i", (iso, year) => {
    expect(yearOf(day(iso))).toBe(year);
  });

  it("has no year for an unknown day", () => {
    expect(yearOf(NO_DAY)).toBeNull();
  });
});

describe("yearRange", () => {
  it("spans the dated points and skips the rest", () => {
    expect(yearRange(Int32Array.from([day("2012-06-01"), NO_DAY, day("2004-01-01")]))).toEqual({
      first: 2004,
      last: 2012,
    });
  });

  it("is null when nothing is dated", () => {
    expect(yearRange(Int32Array.from([NO_DAY]))).toBeNull();
  });
});

describe("year colours", () => {
  it.each(["light", "dark"] as Theme[])(
    "run the %s ramp from the first year to the last",
    (theme) => {
      const range = { first: 2000, last: 2020 };
      const ramp = YEAR_RAMP[theme];

      expect(yearColour(2000, range, theme)).toEqual(hexToRgb(ramp[0] ?? ""));
      expect(yearColour(2020, range, theme)).toEqual(hexToRgb(ramp.at(-1) ?? ""));
    },
  );

  it("darken with newer years on light, and lighten on dark", () => {
    const luma = ([r, g, b]: Rgb) => 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const range = { first: 2000, last: 2020 };
    const years = [2000, 2005, 2010, 2015, 2020];

    const light = years.map((y) => luma(yearColour(y, range, "light")));
    const dark = years.map((y) => luma(yearColour(y, range, "dark")));

    expect(light).toEqual([...light].sort((a, b) => b - a));
    expect(dark).toEqual([...dark].sort((a, b) => a - b));
  });

  it("interpolate between steps", () => {
    expect(rampColour(["#000000", "#ffffff"], 0.5)).toEqual([128, 128, 128]);
  });
});

describe("pointColours", () => {
  const shown = points(
    [day("2004-03-01"), day("2014-03-01"), NO_DAY],
    [MediaKind.photo, MediaKind.video, MediaKind.photo],
  );

  it("colours every point the plain colour with none", () => {
    const colours = pointColours(shown, "none", "light", PLAIN);
    expect([0, 1, 2].map((i) => colourAt(colours, i))).toEqual([PLAIN, PLAIN, PLAIN]);
  });

  it.each(["light", "dark"] as Theme[])("colours each media type on %s", (theme) => {
    const colours = pointColours(shown, "media", theme, PLAIN);
    expect(colourAt(colours, 0)).toEqual(hexToRgb(MEDIA_COLOURS[theme].photo));
    expect(colourAt(colours, 1)).toEqual(hexToRgb(MEDIA_COLOURS[theme].video));
  });

  it.each(["light", "dark"] as Theme[])("colours by year, and undated grey, on %s", (theme) => {
    const colours = pointColours(shown, "year", theme, PLAIN);
    expect(colourAt(colours, 0)).toEqual(hexToRgb(YEAR_RAMP[theme][0] ?? ""));
    expect(colourAt(colours, 1)).toEqual(hexToRgb(YEAR_RAMP[theme].at(-1) ?? ""));
    expect(colourAt(colours, 2)).toEqual(hexToRgb(UNKNOWN_COLOUR[theme]));
  });
});

describe("legend", () => {
  it.each(["light", "dark"] as Theme[])("names each media type in its colour on %s", (theme) => {
    expect(legend(points([0]), "media", theme)).toEqual({
      kind: "swatches",
      entries: [
        { label: "Photo", colour: MEDIA_COLOURS[theme].photo },
        { label: "Video", colour: MEDIA_COLOURS[theme].video },
      ],
    });
  });

  it("shows the year ramp from the first year to the last, and the undated grey", () => {
    const shown = points([day("2004-03-01"), day("2014-03-01"), NO_DAY]);
    const key = legend(shown, "year", "light");

    expect(key).toMatchObject({
      kind: "ramp",
      first: 2004,
      last: 2014,
      unknown: UNKNOWN_COLOUR.light,
    });
    if (key.kind !== "ramp") throw new Error("Not a ramp");
    // The same steps the dots are drawn from, oldest first.
    expect(key.gradient).toBe(`linear-gradient(to right, ${YEAR_RAMP.light.join(", ")})`);
  });

  it("leaves out the undated grey when every point has a date", () => {
    expect(legend(points([day("2004-03-01"), day("2014-03-01")]), "year", "dark")).toMatchObject({
      unknown: null,
    });
  });

  it("shows one swatch for a library from a single year", () => {
    const key = legend(points([day("2020-01-01"), day("2020-08-01")]), "year", "light");
    const dots = pointColours(points([day("2020-01-01")]), "year", "light", PLAIN);
    expect(key).toEqual({
      kind: "swatches",
      entries: [{ label: "2020", colour: YEAR_RAMP.light.at(-1) }],
    });
    expect(colourAt(dots, 0)).toEqual(hexToRgb(YEAR_RAMP.light.at(-1) ?? ""));
  });

  it("is empty with none, or before the points load", () => {
    expect(legend(points([0]), "none", "light")).toEqual({ kind: "none" });
    expect(legend(null, "year", "light")).toEqual({ kind: "none" });
  });
});
