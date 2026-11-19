/** What colours the map's dots (plan §6.4 "Color-by overlays"): nothing, the year
 * an item was taken, or its media type. Each colour is per theme, from the dataviz
 * reference palette, validated for both surfaces and for colour blindness:
 *
 * - **Media type** is categorical: blue for photos, orange for videos, its first two
 *   slots (worst colour-blind ΔE 24.7 light, 26.8 dark; all pairs, as a scatter needs).
 * - **Year** is sequential: one blue ramp from the oldest year to the newest, darker
 *   for newer on the light theme and lighter for newer on the dark one. Its pale end
 *   clears 2:1 against each surface, so no dot fades into the background.
 * - **Unknown** dates are a neutral grey, named in the legend.
 *
 * Colour is the base layer: dimming for search and filters (later issues) applies
 * on top of it, so a dimmed dot stays dimmed whatever its colour. */

import { MediaKind, NO_DAY, type MapPoints } from "./map-points";

export type ColourBy = "none" | "year" | "media";
export const COLOUR_BY: readonly ColourBy[] = ["none", "year", "media"];
export const COLOUR_BY_LABELS: Record<ColourBy, string> = {
  none: "None",
  year: "Year",
  media: "Type",
};

export type Theme = "light" | "dark";
export type Rgb = readonly [number, number, number];

/** The blue ramp's steps 250 to 700 (light) and 600 to 100 (dark): oldest first. */
export const YEAR_RAMP: Record<Theme, readonly string[]> = {
  light: [
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
  ],
  dark: [
    "#184f95",
    "#1c5cab",
    "#256abf",
    "#2a78d6",
    "#3987e5",
    "#5598e7",
    "#6da7ec",
    "#86b6ef",
    "#9ec5f4",
    "#b7d3f6",
    "#cde2fb",
  ],
};

export const MEDIA_COLOURS: Record<Theme, { photo: string; video: string }> = {
  light: { photo: "#2a78d6", video: "#eb6834" },
  dark: { photo: "#3987e5", video: "#d95926" },
};

export const UNKNOWN_COLOUR: Record<Theme, string> = { light: "#9a9fa6", dark: "#6b7076" };

export function hexToRgb(hex: string): Rgb {
  const value = /^#([0-9a-f]{6})$/i.exec(hex)?.[1];
  if (value === undefined) throw new Error(`Not a colour: ${hex}`);
  return [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16)) as unknown as Rgb;
}

/** The calendar year of a capture day (days since 1970-01-01), or null if unknown. */
export function yearOf(day: number): number | null {
  if (day === NO_DAY) return null;
  return new Date(day * 86_400_000).getUTCFullYear();
}

export interface YearRange {
  first: number;
  last: number;
}

/** The first and last years among the points, or null if none has a date. */
export function yearRange(days: Int32Array): YearRange | null {
  let first = Infinity;
  let last = -Infinity;
  for (const day of days) {
    const year = yearOf(day);
    if (year === null) continue;
    first = Math.min(first, year);
    last = Math.max(last, year);
  }
  return first <= last ? { first, last } : null;
}

/** The colour `t` of the way along a ramp (0 the first step, 1 the last). */
export function rampColour(ramp: readonly string[], t: number): Rgb {
  const at = Math.min(Math.max(t, 0), 1) * (ramp.length - 1);
  const low = Math.floor(at);
  const high = Math.min(low + 1, ramp.length - 1);
  const [a, b] = [hexToRgb(ramp[low] ?? "#000000"), hexToRgb(ramp[high] ?? "#000000")];
  const f = at - low;
  return [0, 1, 2].map((i) =>
    Math.round((a[i] ?? 0) + ((b[i] ?? 0) - (a[i] ?? 0)) * f),
  ) as unknown as Rgb;
}

/** A year's colour: the ramp from `range.first` to `range.last`. */
export function yearColour(year: number, range: YearRange, theme: Theme): Rgb {
  const span = range.last - range.first;
  return rampColour(YEAR_RAMP[theme], span === 0 ? 1 : (year - range.first) / span);
}

/** Every point's colour, as RGB triples for deck.gl's `getFillColor`. */
export function pointColours(
  points: MapPoints,
  by: ColourBy,
  theme: Theme,
  plain: Rgb,
): Uint8Array {
  const out = new Uint8Array(points.count * 3);
  const range = by === "year" ? yearRange(points.days) : null;
  const unknown = hexToRgb(UNKNOWN_COLOUR[theme]);
  const photo = hexToRgb(MEDIA_COLOURS[theme].photo);
  const video = hexToRgb(MEDIA_COLOURS[theme].video);
  for (let i = 0; i < points.count; i++) {
    let colour = plain;
    if (by === "media") {
      colour = points.kinds[i] === MediaKind.video ? video : photo;
    } else if (by === "year") {
      const year = yearOf(points.days[i] ?? NO_DAY);
      colour = year === null || range === null ? unknown : yearColour(year, range, theme);
    }
    out.set(colour, 3 * i);
  }
  return out;
}

export type Legend =
  | { kind: "none" }
  | { kind: "swatches"; entries: { label: string; colour: string }[] }
  | {
      kind: "ramp";
      first: number;
      last: number;
      /** A CSS gradient, oldest on the left. */
      gradient: string;
      /** The grey of items with no date, if any are on the map. */
      unknown: string | null;
    };

/** What the legend shows for `by`, given the points on the map. */
export function legend(points: MapPoints | null, by: ColourBy, theme: Theme): Legend {
  if (by === "media") {
    const colours = MEDIA_COLOURS[theme];
    return {
      kind: "swatches",
      entries: [
        { label: "Photo", colour: colours.photo },
        { label: "Video", colour: colours.video },
      ],
    };
  }
  if (by === "year" && points !== null) {
    const range = yearRange(points.days);
    const undated = points.days.includes(NO_DAY);
    if (range === null) {
      return {
        kind: "swatches",
        entries: [{ label: "Unknown date", colour: UNKNOWN_COLOUR[theme] }],
      };
    }
    const unknown = undated ? [{ label: "Unknown date", colour: UNKNOWN_COLOUR[theme] }] : [];
    if (range.first === range.last) {
      const only = YEAR_RAMP[theme].at(-1) ?? "#000000";
      return {
        kind: "swatches",
        entries: [{ label: String(range.first), colour: only }, ...unknown],
      };
    }
    return {
      kind: "ramp",
      first: range.first,
      last: range.last,
      gradient: `linear-gradient(to right, ${YEAR_RAMP[theme].join(", ")})`,
      unknown: undated ? UNKNOWN_COLOUR[theme] : null,
    };
  }
  return { kind: "none" };
}
