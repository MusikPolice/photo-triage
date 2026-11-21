import { expect, test } from "@playwright/test";

import { formatTakenAt, itemFromSearch, type ItemDetails } from "../src/lib/item";
import { mapState, waitUntilProcessed, type MapHook } from "./stack";

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
