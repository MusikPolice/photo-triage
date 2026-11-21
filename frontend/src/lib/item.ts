/** One item, as the lightbox shows it (plan §6.4): its details from
 * `GET /api/items/{id}`, and its picture or video from `GET /api/items/{id}/view`. */

import { getJson } from "./api";
import type { components } from "./api-types";
import type { Locale } from "./format";

export type ItemDetails = components["schemas"]["ItemDetails"];

export function getItem(itemId: number, fetchFn: typeof fetch = fetch): Promise<ItemDetails> {
  return getJson<ItemDetails>(`/items/${String(itemId)}`, fetchFn);
}

/** The picture (a JPEG) or the video (an MP4), whatever the original's format. */
export function viewUrl(itemId: number): string {
  return `/api/items/${String(itemId)}/view`;
}

/** The query that opens the lightbox on an item: `?item=12`. */
export const ITEM_PARAM = "item";

/** The item a URL's query opens in the lightbox, or null. */
export function itemFromSearch(search: string): number | null {
  const value = new URLSearchParams(search).get(ITEM_PARAM);
  if (value === null || !/^[1-9]\d*$/.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) ? id : null;
}

/** `search` with the lightbox open on `itemId`, or closed for null, keeping any
 * other parameters. Starts with `?`, or is empty. */
export function withItem(search: string, itemId: number | null): string {
  const params = new URLSearchParams(search);
  if (itemId === null) params.delete(ITEM_PARAM);
  else params.set(ITEM_PARAM, String(itemId));
  const query = params.toString();
  return query === "" ? "" : `?${query}`;
}

/** A capture time as the camera recorded it, `2019-04-15T10:20:30` with no timezone,
 * shown as that wall-clock time: "15 April 2019 at 10:20". */
export function formatTakenAt(takenAt: string, { locale }: Locale = {}): string {
  // Read and shown as UTC, so the wall-clock time stays as written.
  const at = new Date(`${takenAt}Z`);
  if (Number.isNaN(at.getTime())) return takenAt;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "long",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(at);
}

/** A video's length: "0:07", "3:25", "1:02:03". */
export function formatDuration(seconds: number): string {
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${String(h)}:${String(m).padStart(2, "0")}:${s}` : `${String(m)}:${s}`;
}

/** A file size: "820 kB", "3.1 MB", "1.2 GB" (powers of 1000). */
export function formatBytes(bytes: number, { locale }: Locale = {}): string {
  const units = ["B", "kB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1;
  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(value);
  return `${number} ${units[unit] ?? ""}`;
}

/** "4032 × 3024", or null if either is unknown. */
export function formatDimensions(
  width: number | null,
  height: number | null,
  { locale }: Locale = {},
): string | null {
  if (width === null || height === null) return null;
  const n = new Intl.NumberFormat(locale);
  return `${n.format(width)} × ${n.format(height)}`;
}

/** The file's name, the last part of its path. */
export function fileName(path: string): string {
  return path.split("/").at(-1) ?? path;
}
