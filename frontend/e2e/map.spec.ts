import { expect, test } from "@playwright/test";

import { mapState, waitUntilProcessed } from "./stack";

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
