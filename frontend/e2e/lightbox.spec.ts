import { expect, test } from "@playwright/test";

import { formatTakenAt, itemFromSearch, type ItemDetails } from "../src/lib/item";
import { activity, mapDetail, mapState, waitUntilProcessed, type MapHook } from "./stack";

test("tapping an item opens it in the lightbox, with its date", async ({
  page,
  request,
  isMobile,
}) => {
  await waitUntilProcessed(request);
  await page.goto("/");
  await expect.poll(async () => (await mapState(page)).visible.length).toBeGreaterThan(0);

  // An item with a date, to tap.
  let target: ItemDetails | null = null;
  for (const id of (await mapState(page)).visible) {
    const item = (await (await request.get(`/api/items/${String(id)}`)).json()) as ItemDetails;
    if (item.taken_at_local !== null && item.media_type === "photo") {
      target = item;
      break;
    }
  }
  if (target === null) throw new Error("No dated photo on the map");
  const id = target.id;
  const at = await page.evaluate(
    (itemId) =>
      (window as unknown as { __photoTriageMap: MapHook }).__photoTriageMap.pagePosition(itemId),
    id,
  );
  if (at === null) throw new Error(`Item ${String(id)} isn't on the map`);

  if (isMobile) await page.touchscreen.tap(at[0], at[1]);
  else await page.mouse.click(at[0], at[1]);

  const lightbox = page.getByRole("dialog");
  await expect(lightbox).toBeVisible();
  // Another item may sit under the same spot: check the one that opened.
  const opened = itemFromSearch(new URL(page.url()).search);
  expect(opened).not.toBeNull();
  const shown = (await (await request.get(`/api/items/${String(opened)}`)).json()) as ItemDetails;
  const date =
    shown.taken_at_local === null
      ? "Unknown date"
      : formatTakenAt(shown.taken_at_local, {
          locale: await page.evaluate(() => navigator.language),
        });
  await expect(lightbox.getByRole("heading", { level: 2 })).toHaveText(date);
  await expect(lightbox.locator("img")).toHaveJSProperty("complete", true);
  expect(await lightbox.locator("img").evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(
    shown.width,
  );

  // A reload reopens it; the back button closes it.
  await page.reload();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("dialog")).toBeHidden();
  expect(new URL(page.url()).search).toBe("");
});

const THUMBNAIL_OFFSET_PX = 14;

test("tapping a thumbnail opens its item", async ({ page, request, isMobile }) => {
  const done = await waitUntilProcessed(request);
  await expect
    .poll(async () => (await activity(request)).atlas_version, { timeout: 60_000 })
    .toBe(done.layout_version);
  await page.goto("/");
  await expect.poll(async () => (await mapDetail(page)).atlasVersion).toBe(done.layout_version);
  if ((await mapDetail(page)).detail !== "thumbnails") {
    const box = await page.locator(".map canvas").boundingBox();
    if (box === null) throw new Error("No map canvas");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -400);
  }
  await expect.poll(async () => (await mapDetail(page)).detail).toBe("thumbnails");
  // Thumbnails are drawn once their sheet has loaded.
  await expect.poll(async () => (await mapDetail(page)).sheets).toEqual([0]);
  await expect.poll(async () => (await mapDetail(page)).loaded).toBe(true);

  // An item whose thumbnail is at least 36 px both ways, to tap off its centre.
  const visible = (await mapState(page)).visible;
  let id: number | null = null;
  for (const candidate of visible) {
    const item = (await (
      await request.get(`/api/items/${String(candidate)}`)
    ).json()) as ItemDetails;
    if (item.width !== null && item.height !== null) {
      const ratio = Math.min(item.width, item.height) / Math.max(item.width, item.height);
      if (ratio >= 0.75) {
        id = candidate;
        break;
      }
    }
  }
  if (id === null) throw new Error("No item near 4:3 on the map");
  const at = await page.evaluate(
    (itemId) =>
      (window as unknown as { __photoTriageMap: MapHook }).__photoTriageMap.pagePosition(itemId),
    id,
  );
  if (at === null) throw new Error(`Item ${String(id)} isn't on the map`);
  // Off its centre, within the thumbnail (48 px on its long side) but out of reach
  // of the dot under it, so it's the thumbnail that's tapped.
  const [x, y] = [at[0] + THUMBNAIL_OFFSET_PX, at[1] + THUMBNAIL_OFFSET_PX];
  if (isMobile) await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);

  await expect(page.getByRole("dialog")).toBeVisible();
  // A thumbnail's pick is an index into its sheet's points: it must map back to
  // the item tapped.
  expect(itemFromSearch(new URL(page.url()).search)).toBe(id);
});
