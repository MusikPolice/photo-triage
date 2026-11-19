import { expect, test } from "@playwright/test";

import { stageName } from "../src/lib/format";
import { waitUntilProcessed } from "./stack";

test("the Activity page shows every stage done", async ({ page, request }) => {
  await page.goto("/activity");
  const done = await waitUntilProcessed(request);

  await expect(page.getByText("Nothing queued.")).toBeVisible();
  const idle = page.locator("ul.stages.idle > li");
  for (const stage of done.stages.filter((s) => s.done > 0)) {
    const row = idle.filter({ has: page.getByText(stageName(stage.stage), { exact: true }) });
    await expect(row).toContainText(`${String(stage.done)} done`);
  }
  // Every item stage ran: scan, metadata, thumbnails and CLIP at least.
  for (const name of ["scan", "metadata_read", "thumbnail", "clip"]) {
    expect(done.stages.find((s) => s.stage === name)?.done ?? 0).toBeGreaterThan(0);
  }
});
