/** The trash (plan §6.8): moving items in and out, what's in it, and how the Trash
 * page words and selects it. */

import { getJson, postJson } from "./api";
import type { components } from "./api-types";
import type { Locale } from "./format";

export type TrashedItem = components["schemas"]["TrashedItem"];
export type TrashPage = components["schemas"]["TrashPage"];
export type Moved = components["schemas"]["Moved"];

/** Items the Trash page loads at a time. */
export const TRASH_PAGE_SIZE = 100;

export function trashItems(itemIds: number[], fetchFn: typeof fetch = fetch): Promise<Moved> {
  return postJson<Moved>("/trash", fetchFn, { item_ids: itemIds });
}

export function restoreItems(itemIds: number[], fetchFn: typeof fetch = fetch): Promise<Moved> {
  return postJson<Moved>("/trash/restore", fetchFn, { item_ids: itemIds });
}

/** What's in the trash, the most recently trashed first. */
export function getTrash(
  offset = 0,
  limit: number = TRASH_PAGE_SIZE,
  fetchFn: typeof fetch = fetch,
): Promise<TrashPage> {
  return getJson<TrashPage>(`/trash?limit=${String(limit)}&offset=${String(offset)}`, fetchFn);
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** When an item leaves the trash for good: "purged in 12 days", "purged within a
 * day", or "purged soon" once its date has passed and the purge hasn't run yet. */
export function purgeWording(purgeAfter: string, now: Date): string {
  const left = new Date(purgeAfter).getTime() - now.getTime();
  if (Number.isNaN(left)) return "";
  if (left <= 0) return "purged soon";
  if (left < DAY_MS) return "purged within a day";
  const days = Math.round(left / DAY_MS);
  return `purged in ${String(days)} ${days === 1 ? "day" : "days"}`;
}

/** A moment as a date in the browser's timezone: "3 Nov 2026". */
export function formatDay(at: string, { locale }: Locale = {}): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return at;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(date);
}

/** The selection with `id` added, or taken out if it was in. */
export function toggled(selection: ReadonlySet<number>, id: number): Set<number> {
  const next = new Set(selection);
  if (!next.delete(id)) next.add(id);
  return next;
}

/** Whether every one of `ids` is selected, and there's at least one. */
export function allSelected(selection: ReadonlySet<number>, ids: readonly number[]): boolean {
  return ids.length > 0 && ids.every((id) => selection.has(id));
}

/** "Select all": every one of `ids`, or none if they all were. */
export function toggledAll(selection: ReadonlySet<number>, ids: readonly number[]): Set<number> {
  return allSelected(selection, ids) ? new Set() : new Set(ids);
}

/** The selection, keeping only `ids`: those still listed. */
export function within(selection: ReadonlySet<number>, ids: readonly number[]): Set<number> {
  return new Set(ids.filter((id) => selection.has(id)));
}

function items(count: number): string {
  return `${String(count)} ${count === 1 ? "item" : "items"}`;
}

/** What a move did, for people: "Restored 3 items." and why any weren't. */
export function movedSummary(verb: string, result: Moved): string {
  const parts: string[] = [];
  if (result.moved.length > 0 || result.skipped.length === 0) {
    parts.push(`${verb} ${items(result.moved.length)}.`);
  }
  if (result.skipped.length > 0) {
    const reasons = [...new Set(result.skipped.map((s) => s.reason))].join("; ");
    parts.push(`${items(result.skipped.length)} couldn't be: ${reasons}.`);
  }
  return parts.join(" ");
}
