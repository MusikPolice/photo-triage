import { expect, test } from "@playwright/test";

import { activity, mapDetail, mapState, waitUntilProcessed } from "./stack";

test("the map shows one point per embedded fixture", async ({ page, request }) => {
  const done = await waitUntilProcessed(request);
  const clip = done.stages.find((s) => s.stage === "clip");
  expect(clip?.done).toBeGreaterThan(0);

  await page.goto("/");

  await expect.poll(async () => (await mapState(page)).version).toBe(done.layout_version);
  const state = await mapState(page);
  // The first view fits every point.
  expect(state.visible).toHaveLength(clip?.done ?? -1);
  expect(new Set(state.visible).size).toBe(state.visible.length);
  expect(state.zoom).not.toBeNull();
});

test("zoomed in, the map draws thumbnails from the atlas sheets", async ({ page, request }) => {
  const done = await waitUntilProcessed(request);
  // The atlases are drawn after each layout fit.
  await expect
    .poll(async () => (await activity(request)).atlas_version, { timeout: 60_000 })
    .toBe(done.layout_version);
  const sheet = page.waitForResponse(
    (response) => /\/api\/map\/atlases\/\d+\/\d+\.jpg$/.test(response.url()) && response.ok(),
  );

  await page.goto("/");
  await expect.poll(async () => (await mapDetail(page)).atlasVersion).toBe(done.layout_version);
  const before = (await mapState(page)).zoom ?? 0;
  const box = await page.locator(".map canvas").boundingBox();
  if (box === null) throw new Error("No map canvas");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, -400);

  await expect.poll(async () => (await mapState(page)).zoom ?? 0).toBeGreaterThan(before);
  await expect.poll(async () => (await mapDetail(page)).detail).toBe("thumbnails");
  // The tier-1 library fits on one sheet.
  await expect.poll(async () => (await mapDetail(page)).sheets).toEqual([0]);
  expect((await sheet).headers()["content-type"]).toBe("image/jpeg");
});
