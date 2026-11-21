/* Helpers for the end-to-end tests: the stack's API, read as the page reads it. */
import { expect, type APIRequestContext, type Page } from "@playwright/test";

import type { Activity } from "../src/lib/api";

/** How long the worker may take over the tier-1 fixtures, layout included. */
export const PROCESSED_TIMEOUT_MS = 180_000;

export async function activity(request: APIRequestContext): Promise<Activity> {
  const response = await request.get("/api/activity");
  expect(response.ok()).toBe(true);
  return (await response.json()) as Activity;
}

/** Whether every stage has no work left, and the layout has been fitted. */
export function processed(current: Activity): boolean {
  const idle = current.stages.every((s) => s.pending + s.running + s.errored === 0);
  return idle && current.layout_version !== null;
}

/** Wait until the worker has processed the library, and return the activity then. */
export async function waitUntilProcessed(request: APIRequestContext): Promise<Activity> {
  await expect
    .poll(async () => processed(await activity(request)), {
      timeout: PROCESSED_TIMEOUT_MS,
      intervals: [1000],
    })
    .toBe(true);
  return activity(request);
}

/** The map's test hook (LibraryMap.svelte, dev-environment §6). */
export interface MapHook {
  version: number | null;
  zoom: number | null;
  atlasVersion: number | null;
  detail: "dots" | "thumbnails";
  sheets: number[];
  loaded: boolean;
  visibleIds(): number[];
  pagePosition(itemId: number): [number, number] | null;
}

/** How the map is drawn: dots or thumbnails, from which atlas sheets, and whether
 * every layer has what it draws (a sheet's image loads after its layer is made). */
export async function mapDetail(page: Page): Promise<{
  atlasVersion: number | null;
  detail: string | null;
  sheets: number[];
  loaded: boolean;
}> {
  return page.evaluate(() => {
    const hook = (window as unknown as { __photoTriageMap?: MapHook }).__photoTriageMap;
    if (hook === undefined) return { atlasVersion: null, detail: null, sheets: [], loaded: false };
    return {
      atlasVersion: hook.atlasVersion,
      detail: hook.detail,
      sheets: hook.sheets,
      loaded: hook.loaded,
    };
  });
}

export async function mapState(
  page: Page,
): Promise<{ version: number | null; zoom: number | null; visible: number[] }> {
  return page.evaluate(() => {
    const hook = (window as unknown as { __photoTriageMap?: MapHook }).__photoTriageMap;
    if (hook === undefined) return { version: null, zoom: null, visible: [] };
    return { version: hook.version, zoom: hook.zoom, visible: hook.visibleIds() };
  });
}
