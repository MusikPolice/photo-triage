import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import type { components } from "../src/lib/api-types";
import { insidePolygon, type Vertex } from "../src/lib/lasso";
import type { TrashPage } from "../src/lib/trash";
import { mapState, pagePosition, waitUntilProcessed, type MapHook } from "./stack";

type WriteLog = components["schemas"]["WriteLog"];

async function selectedIds(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    (window as unknown as { __photoTriageMap: MapHook }).__photoTriageMap.selectedIds(),
  );
}

async function trashed(request: APIRequestContext): Promise<number[]> {
  const trash = (await (await request.get("/api/trash")).json()) as TrashPage;
  return trash.items.map((item) => item.id).sort((a, b) => a - b);
}

/** The map with everything on it, ready to lasso. */
async function openMap(page: Page, request: APIRequestContext): Promise<void> {
  await waitUntilProcessed(request);
  await page.goto("/");
  await expect.poll(async () => (await mapState(page)).visible.length).toBeGreaterThan(0);
}

/** Lasso the map below the panels, as a concave outline with a notch cut into its
 * top, and return what the hook's positions say it holds. */
async function lassoBelowPanels(page: Page): Promise<number[]> {
  await page.getByRole("button", { name: "Lasso" }).click();
  await expect(page.getByRole("button", { name: "Lasso" })).toHaveAttribute("aria-pressed", "true");
  const map = await page.locator(".map").boundingBox();
  const panel = await page.locator(".panel").boundingBox();
  if (map === null || panel === null) throw new Error("No map");
  const [left, right] = [map.x + 10, map.x + map.width - 10];
  const top = panel.y + panel.height + 10;
  const bottom = map.y + map.height - 10;
  const middle = (left + right) / 2;
  const outline: Vertex[] = [
    [left, top],
    [middle - 40, top],
    [middle, (top + bottom) / 2],
    [middle + 40, top],
    [right, top],
    [right, bottom],
    [left, bottom],
  ];

  const inside: number[] = [];
  for (const id of (await mapState(page)).visible) {
    const at = await pagePosition(page, id);
    if (at !== null && insidePolygon(at[0], at[1], outline)) inside.push(id);
  }
  expect(inside.length).toBeGreaterThan(0);

  const [start, ...rest] = outline;
  if (start === undefined) throw new Error("No outline");
  await page.mouse.move(...start);
  await page.mouse.down();
  for (const [x, y] of [...rest, start]) await page.mouse.move(x, y, { steps: 8 });
  await page.mouse.up();
  return inside.sort((a, b) => a - b);
}

test.describe("on desktop", () => {
  test.skip(({ isMobile }) => isMobile, "The lasso is for desktop");

  test("a lassoed selection moves to the trash, and undo brings it back", async ({
    page,
    request,
  }) => {
    await openMap(page, request);
    const inside = await lassoBelowPanels(page);

    expect(await selectedIds(page)).toEqual(inside);
    const bar = page.getByRole("toolbar", { name: "Selection" });
    await expect(bar).toContainText(`${String(inside.length)} selected`);

    await bar.getByRole("button", { name: "Move to trash" }).click();

    const items = `${String(inside.length)} ${inside.length === 1 ? "item" : "items"}`;
    await expect(
      page.getByRole("status").filter({ hasText: `Moved ${items} to the trash.` }),
    ).toBeVisible();
    for (const id of inside) await expect.poll(() => pagePosition(page, id)).toBeNull();
    expect(await trashed(request)).toEqual(inside);
    await expect(bar).toBeHidden();

    await page.getByRole("button", { name: "Undo" }).click();

    await expect(page.getByRole("status").filter({ hasText: `Restored ${items}.` })).toBeVisible();
    for (const id of inside) await expect.poll(() => pagePosition(page, id)).not.toBeNull();
    expect(await trashed(request)).toEqual([]);
  });

  test("Add tag queues a write for each selected item, and no others", async ({
    page,
    request,
  }) => {
    await openMap(page, request);
    const inside = await lassoBelowPanels(page);
    const tag = "e2e bulk";
    const bar = page.getByRole("toolbar", { name: "Selection" });
    const items = `${String(inside.length)} ${inside.length === 1 ? "item" : "items"}`;

    await bar.getByRole("textbox", { name: "Tag" }).fill(tag);
    await bar.getByRole("button", { name: "Add tag" }).click();
    try {
      await expect(bar.getByRole("status")).toHaveText(`Tagged ${items} “${tag}”.`);
      const log = (await (await request.get("/api/metadata-writes?limit=500")).json()) as WriteLog;
      const tagged = log.items
        .filter((write) => (write.fields["tags"] as string[] | undefined)?.includes(tag))
        .map((write) => write.item_id)
        .sort((a, b) => a - b);
      expect(tagged).toEqual(inside);
    } finally {
      await bar.getByRole("button", { name: "Remove tag" }).click();
      await expect(bar.getByRole("status")).toHaveText(`Removed “${tag}” from ${items}.`);
    }
  });
});

test("the phone has no lasso", async ({ page, request, isMobile }) => {
  test.skip(!isMobile, "The phone project only");
  await openMap(page, request);
  await expect(page.getByRole("search")).toBeVisible();
  await expect(page.getByRole("button", { name: "Lasso" })).toHaveCount(0);
  expect(
    await page.evaluate(
      () => (window as unknown as { __photoTriageMap: MapHook }).__photoTriageMap.lassoAvailable,
    ),
  ).toBe(false);
});
