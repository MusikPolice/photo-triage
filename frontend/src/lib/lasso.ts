/** Lasso select on the map, and the bulk actions on what's selected (plan §6.4,
 * desktop only). An outline drawn on the screen is turned into map units, and the
 * items inside it are selected. With a search or a date range, only the items
 * they leave highlighted can be: a dimmed item isn't picked up. */

import { postJson } from "./api";
import type { components } from "./api-types";
import type { MapPoints, MapView } from "./map-points";
import { restoreItems, trashItems, type Moved } from "./trash";

/** A point as [x, y]: in map units, or in pixels from the map's top left. */
export type Vertex = readonly [number, number];

/** Whether (x, y) is inside the polygon, by the even-odd rule, so a concave outline
 * works, and one that crosses itself leaves out what it loops round twice. */
export function insidePolygon(x: number, y: number, polygon: readonly Vertex[]): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i] ?? [0, 0];
    const [xj, yj] = polygon[j] ?? [0, 0];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** A pixel on a `width` × `height` map, from its top left, in map units. */
export function toMap(pixel: Vertex, view: MapView, width: number, height: number): Vertex {
  const scale = 2 ** view.zoom;
  return [
    view.target[0] + (pixel[0] - width / 2) / scale,
    view.target[1] + (pixel[1] - height / 2) / scale,
  ];
}

/** The ids of the points inside `outline` (in pixels on the map), in id order. With
 * `mask`, only those it highlights. An outline of fewer than 3 points holds none. */
export function lassoed(
  points: MapPoints,
  outline: readonly Vertex[],
  view: MapView,
  width: number,
  height: number,
  mask: Uint8Array | null,
): number[] {
  if (outline.length < 3) return [];
  const polygon = outline.map((pixel) => toMap(pixel, view, width, height));
  const box = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  for (const [x, y] of polygon) {
    box.minX = Math.min(box.minX, x);
    box.maxX = Math.max(box.maxX, x);
    box.minY = Math.min(box.minY, y);
    box.maxY = Math.max(box.maxY, y);
  }
  const ids: number[] = [];
  for (let i = 0; i < points.count; i++) {
    if (mask !== null && mask[i] !== 1) continue;
    const x = points.positions[2 * i] ?? NaN;
    const y = points.positions[2 * i + 1] ?? NaN;
    if (x < box.minX || x > box.maxX || y < box.minY || y > box.maxY) continue;
    if (insidePolygon(x, y, polygon)) ids.push(points.ids[i] ?? 0);
  }
  return ids.sort((a, b) => a - b);
}

/** The selection after a lasso: the items lassoed, or with Shift, those added to it. */
export function selectionAfter(
  selection: ReadonlySet<number>,
  ids: readonly number[],
  adding: boolean,
): Set<number> {
  return adding ? new Set([...selection, ...ids]) : new Set(ids);
}

/** The selection, keeping only the items still on the map. The same set if it
 * keeps them all. */
export function onMap(selection: ReadonlySet<number>, points: MapPoints): ReadonlySet<number> {
  if (selection.size === 0) return selection;
  const present = new Set(points.ids);
  const kept = [...selection].filter((id) => present.has(id));
  return kept.length === selection.size ? selection : new Set(kept);
}

/** The point indices of the selected items, for drawing them. */
export function selectedIndices(points: MapPoints, selection: ReadonlySet<number>): number[] {
  if (selection.size === 0) return [];
  const indices: number[] = [];
  points.ids.forEach((id, i) => {
    if (selection.has(id)) indices.push(i);
  });
  return indices;
}

/** The outline as an SVG path, closed. */
export function outlinePath(outline: readonly Vertex[]): string {
  if (outline.length === 0) return "";
  return `${outline.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join("")}Z`;
}

/** Items the API takes in one request (`MAX_ITEMS` in the trash and tags routes). */
export const MAX_ITEMS_PER_REQUEST = 10_000;

/** The longest tag the API takes (`MAX_TAG_CHARS` in the tags route). */
export const MAX_TAG_CHARS = 100;

function chunks(ids: readonly number[]): number[][] {
  const out: number[][] = [];
  for (let i = 0; i < ids.length; i += MAX_ITEMS_PER_REQUEST) {
    out.push(ids.slice(i, i + MAX_ITEMS_PER_REQUEST));
  }
  return out;
}

async function moveAll(
  move: (ids: number[], fetchFn: typeof fetch) => Promise<Moved>,
  ids: readonly number[],
  fetchFn: typeof fetch,
): Promise<Moved> {
  const result: Moved = { moved: [], skipped: [] };
  for (const chunk of chunks(ids)) {
    const part = await move(chunk, fetchFn);
    result.moved.push(...part.moved);
    result.skipped.push(...part.skipped);
  }
  return result;
}

/** Trash every one of `ids`, a request per `MAX_ITEMS_PER_REQUEST`. */
export function trashAll(ids: readonly number[], fetchFn: typeof fetch = fetch): Promise<Moved> {
  return moveAll(trashItems, ids, fetchFn);
}

/** Restore every one of `ids` from the trash, likewise. */
export function restoreAll(ids: readonly number[], fetchFn: typeof fetch = fetch): Promise<Moved> {
  return moveAll(restoreItems, ids, fetchFn);
}

export type TagAction = components["schemas"]["TagAction"];
export type TagsChanged = components["schemas"]["TagsChanged"];

/** Add a tag to every one of `ids`, or remove it, a request per
 * `MAX_ITEMS_PER_REQUEST`. Each item it changes gets a metadata write. */
export async function changeTag(
  ids: readonly number[],
  tag: string,
  action: TagAction,
  fetchFn: typeof fetch = fetch,
): Promise<TagsChanged> {
  const result: TagsChanged = { changed: [], unchanged: [], skipped: [] };
  for (const chunk of chunks(ids)) {
    const part = await postJson<TagsChanged>("/tags", fetchFn, { item_ids: chunk, tag, action });
    result.changed.push(...part.changed);
    result.unchanged.push(...part.unchanged);
    result.skipped.push(...part.skipped);
  }
  return result;
}

function items(count: number): string {
  return `${count.toLocaleString()} ${count === 1 ? "item" : "items"}`;
}

function skippedPart(skipped: Moved["skipped"]): string {
  const reasons = [...new Set(skipped.map((s) => s.reason))].join("; ");
  return `${items(skipped.length)} couldn't be: ${reasons}.`;
}

/** What a bulk trash did, for the toast: "Moved 3 items to the trash." */
export function trashSummary(result: Moved): string {
  const parts: string[] = [];
  if (result.moved.length > 0 || result.skipped.length === 0) {
    parts.push(`Moved ${items(result.moved.length)} to the trash.`);
  }
  if (result.skipped.length > 0) parts.push(skippedPart(result.skipped));
  return parts.join(" ");
}

/** Why a tag can't be used, as the API would say, or null if it can. */
export function tagProblem(tag: string): string | null {
  const trimmed = tag.trim();
  if (trimmed === "") return "Type a tag first.";
  if (trimmed.length > MAX_TAG_CHARS) {
    return `A tag can't be longer than ${String(MAX_TAG_CHARS)} characters.`;
  }
  if (trimmed.includes(",")) return "A tag can't contain a comma.";
  return null;
}

/** What a tag change did, for people: "Tagged 3 items “beach”." and what it left. */
export function tagSummary(action: TagAction, tag: string, result: TagsChanged): string {
  const parts = [
    action === "add"
      ? `Tagged ${items(result.changed.length)} “${tag}”.`
      : `Removed “${tag}” from ${items(result.changed.length)}.`,
  ];
  if (result.unchanged.length > 0) {
    parts.push(
      action === "add"
        ? `${items(result.unchanged.length)} already had it.`
        : `${items(result.unchanged.length)} didn't have it.`,
    );
  }
  if (result.skipped.length > 0) parts.push(skippedPart(result.skipped));
  return parts.join(" ");
}

/** "3 selected". */
export function selectedCount(count: number): string {
  return `${count.toLocaleString()} selected`;
}
