/** The map's date-range filter (plan §6.4 "Filters"): a range of months across the
 * library's capture dates. Narrowed, it dims the items outside it, and the items
 * with no capture date, since none can be said to fall within it. It stacks with
 * search: what's highlighted is in range and, with a search, matches it. */

import { NO_DAY, type MapPoints } from "./map-points";
import type { Highlight } from "./search";

/** Months since January 1970, so a month is one step of the slider. */
export type Month = number;

/** The month a capture day (days since 1970-01-01) falls in, or null if unknown. */
export function monthOf(day: number): Month | null {
  if (day === NO_DAY) return null;
  const date = new Date(day * 86_400_000);
  return (date.getUTCFullYear() - 1970) * 12 + date.getUTCMonth();
}

export interface MonthRange {
  /** The first month, inclusive. */
  from: Month;
  /** The last month, inclusive. */
  to: Month;
}

/** The months the points' capture dates span, or null if none has a date. */
export function monthSpan(days: Int32Array): MonthRange | null {
  let from = Infinity;
  let to = -Infinity;
  for (const day of days) {
    const month = monthOf(day);
    if (month === null) continue;
    from = Math.min(from, month);
    to = Math.max(to, month);
  }
  return from <= to ? { from, to } : null;
}

/** Whether `range` leaves out any of `span`: if not, there's no filter. */
export function narrowed(range: MonthRange | null, span: MonthRange | null): boolean {
  if (range === null || span === null) return false;
  return range.from > span.from || range.to < span.to;
}

/** 1 for each point in `range`, or null if the range doesn't narrow the map. */
export function dateMask(
  points: MapPoints,
  range: MonthRange | null,
  span: MonthRange | null,
): Uint8Array | null {
  if (range === null || !narrowed(range, span)) return null;
  const mask = new Uint8Array(points.count);
  for (let i = 0; i < points.count; i++) {
    const month = monthOf(points.days[i] ?? NO_DAY);
    if (month !== null && month >= range.from && month <= range.to) mask[i] = 1;
  }
  return mask;
}

/** What the map highlights, from a search and the date filter, either of which may
 * be off (null). `matches` are the search's matches still in range, best first,
 * which get a ring; with no search there are none, and only the dimming shows. */
export interface Shown {
  /** 1 for each point highlighted; the rest are dimmed. */
  mask: Uint8Array;
  /** The highlighted items, best first with a search, else in the points' order. */
  ids: number[];
  /** A search's matches in range, by point index, best first; null without one. */
  matches: number[] | null;
}

export function shown(
  points: MapPoints,
  search: Highlight | null,
  dates: Uint8Array | null,
): Shown | null {
  if (search === null) {
    if (dates === null) return null;
    const ids: number[] = [];
    for (let i = 0; i < points.count; i++) if (dates[i] === 1) ids.push(points.ids[i] ?? 0);
    return { mask: dates, ids, matches: null };
  }
  if (dates === null) return { mask: search.mask, ids: search.ids, matches: search.indices };
  const mask = new Uint8Array(points.count);
  const matches: number[] = [];
  const ids: number[] = [];
  search.indices.forEach((i, n) => {
    if (dates[i] !== 1) return;
    mask[i] = 1;
    matches.push(i);
    ids.push(search.ids[n] ?? 0);
  });
  return { mask, ids, matches };
}

/** "Mar 2014". */
export function formatMonth(month: Month, locale?: string): string {
  const date = new Date(Date.UTC(1970 + Math.floor(month / 12), month % 12, 1));
  return date.toLocaleDateString(locale, { month: "short", year: "numeric", timeZone: "UTC" });
}

/** The month at `fraction` of the way along `span` (0 its first, 1 its last). */
export function monthAt(fraction: number, span: MonthRange): Month {
  const clamped = Math.min(Math.max(fraction, 0), 1);
  return span.from + Math.round(clamped * (span.to - span.from));
}

/** Where `month` is along `span`, from 0 to 1. */
export function fractionOf(month: Month, span: MonthRange): number {
  const width = span.to - span.from;
  return width === 0 ? 0 : (month - span.from) / width;
}

/** The end of `range` nearer `month`, which a tap or drag there moves. A tie goes
 * to the end the month is beyond, else the start. */
export function nearerEnd(range: MonthRange, month: Month): "from" | "to" {
  const toFrom = Math.abs(month - range.from);
  const toTo = Math.abs(month - range.to);
  return toTo < toFrom || (toTo === toFrom && month > range.to) ? "to" : "from";
}

/** Move whichever end of `range` is nearer `month` to it: a tap on the track. */
export function moveNearer(range: MonthRange, month: Month): MonthRange {
  return moveEnd(range, nearerEnd(range, month), month);
}

/** Move one end, keeping it from passing the other. */
export function moveEnd(range: MonthRange, end: "from" | "to", month: Month): MonthRange {
  return end === "from"
    ? { from: Math.min(month, range.to), to: range.to }
    : { from: range.from, to: Math.max(month, range.from) };
}
