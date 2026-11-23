/** Searching the map with text (plan §6.4 "Search", §6.5): `GET /api/search` returns
 * the matching items, best first, and the map highlights them in place. Matches
 * keep their colour and the rest dim, so the layout, and your memory of it, stays
 * as it was. Dimming is on top of the colour overlay (`map-colours`). */

import type { components } from "./api-types";
import type { MapPoints } from "./map-points";

export type SearchResults = components["schemas"]["SearchResults"];

export const SEARCH_URL = "/api/search";

/** A search that failed: the message says why, as the API put it when it could. */
export class SearchError extends Error {}

export async function searchItems(
  query: string,
  fetchFn: typeof fetch = fetch,
): Promise<SearchResults> {
  const response = await fetchFn(`${SEARCH_URL}?q=${encodeURIComponent(query)}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail;
    } catch {
      // Not JSON: the status says enough.
    }
    throw new SearchError(
      typeof detail === "string" ? detail : `Search failed with HTTP ${String(response.status)}`,
    );
  }
  return (await response.json()) as SearchResults;
}

/** The matches among the map's points. */
export interface Highlight {
  /** 1 for each point that matches, by point index. */
  mask: Uint8Array;
  /** The matching points' indices, best first. */
  indices: number[];
  /** Their item ids, likewise. */
  ids: number[];
}

/** Which of `points` match. A match that isn't on the map (embedded since the
 * layout) isn't counted, since there's nothing to show for it. */
export function highlight(points: MapPoints, results: SearchResults): Highlight {
  const indexOf = new Map<number, number>();
  points.ids.forEach((id, i) => indexOf.set(id, i));
  const mask = new Uint8Array(points.count);
  const indices: number[] = [];
  const ids: number[] = [];
  for (const { id } of results.matches) {
    const i = indexOf.get(id);
    if (i === undefined || mask[i] === 1) continue;
    mask[i] = 1;
    indices.push(i);
    ids.push(id);
  }
  return { mask, indices, ids };
}

/** How opaque a dot or thumbnail that doesn't match is, out of 255. */
export const DIMMED_ALPHA = 46;

/** Each point's RGBA, from its RGB colour: opaque, or dimmed if there's a search and
 * it doesn't match. */
export function shade(colours: Uint8Array, mask: Uint8Array | null): Uint8Array {
  const count = colours.length / 3;
  const out = new Uint8Array(count * 4);
  for (let i = 0; i < count; i++) {
    out[4 * i] = colours[3 * i] ?? 0;
    out[4 * i + 1] = colours[3 * i + 1] ?? 0;
    out[4 * i + 2] = colours[3 * i + 2] ?? 0;
    out[4 * i + 3] = mask === null || mask[i] === 1 ? 255 : DIMMED_ALPHA;
  }
  return out;
}

/** A thumbnail's tint: white keeps the picture as it is, and the alpha dims it. */
export function thumbnailTint(
  mask: Uint8Array | null,
  i: number,
): [number, number, number, number] {
  return [255, 255, 255, mask === null || mask[i] === 1 ? 255 : DIMMED_ALPHA];
}

/** "1 match", "12 matches", "No matches". */
export function matchCount(count: number, locale?: string): string {
  if (count === 0) return "No matches";
  return `${count.toLocaleString(locale)} ${count === 1 ? "match" : "matches"}`;
}
