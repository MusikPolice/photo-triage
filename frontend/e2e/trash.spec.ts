import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { fileName, type ItemDetails } from "../src/lib/item";
import type { TrashPage } from "../src/lib/trash";
import { mapState, pagePosition, waitUntilProcessed } from "./stack";

/** An item on the map, opened in the lightbox. Each test restores what it trashes,
 * so the tests after it find the library as it was. */
async function openAnItem(page: Page, request: APIRequestContext): Promise<ItemDetails> {
  await waitUntilProcessed(request);
  await page.goto("/");
  await expect.poll(async () => (await mapState(page)).visible.length).toBeGreaterThan(0);
  const [id] = (await mapState(page)).visible;
  if (id === undefined) throw new Error("Nothing on the map");
  const item = (await (await request.get(`/api/items/${String(id)}`)).json()) as ItemDetails;
  await page.goto(`/?item=${String(id)}`);
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect.poll(() => pagePosition(page, id)).not.toBeNull();
  return item;
}

async function trashed(request: APIRequestContext): Promise<number[]> {
  const page = (await (await request.get("/api/trash")).json()) as TrashPage;
  return page.items.map((item) => item.id);
}

test("an item trashed from the lightbox leaves the map, and a restore brings it back", async ({
  page,
  request,
}) => {
  const item = await openAnItem(page, request);

  await page.getByRole("button", { name: "Move to trash" }).click();

  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.getByRole("status").filter({ hasText: "Moved to the trash." })).toBeVisible();
  await expect.poll(() => pagePosition(page, item.id)).toBeNull();
  expect(await trashed(request)).toEqual([item.id]);

  // It's on the Trash page, with its thumbnail.
  await page.locator("header").getByRole("link", { name: "Trash" }).click();
  await expect(page.getByRole("heading", { name: "Trash" })).toBeVisible();
  const name = fileName(item.path);
  const card = page.getByRole("listitem").filter({ hasText: name });
  await expect(card).toBeVisible();
  await expect(card.locator("img")).toHaveJSProperty("complete", true);
  expect(
    await card.locator("img").evaluate((img: HTMLImageElement) => img.naturalWidth),
  ).toBeGreaterThan(0);

  await card.getByRole("button", { name: `Restore ${name}` }).click();

  await expect(page.getByRole("status")).toHaveText("Restored 1 item.");
  await expect(card).toBeHidden();
  await expect(page.getByText("Nothing in the trash.")).toBeVisible();
  expect(await trashed(request)).toEqual([]);

  // Back on the map.
  await page.locator("header").getByRole("link", { name: "photo-triage" }).click();
  // The map mounts again first: its hook isn't there until it has.
  await expect.poll(async () => (await mapState(page)).visible.length).toBeGreaterThan(0);
  await expect.poll(() => pagePosition(page, item.id)).not.toBeNull();
});

test("undo in the toast restores a trashed item", async ({ page, request }) => {
  const item = await openAnItem(page, request);

  await page.getByRole("button", { name: "Move to trash" }).click();
  await expect.poll(() => pagePosition(page, item.id)).toBeNull();

  await page.getByRole("button", { name: "Undo" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Restored." })).toBeVisible();
  await expect.poll(() => pagePosition(page, item.id)).not.toBeNull();
  expect(await trashed(request)).toEqual([]);
  expect((await request.get(`/api/items/${String(item.id)}`)).ok()).toBe(true);
});
