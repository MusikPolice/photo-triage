import { expect, test } from "@playwright/test";

import type { ItemDetails } from "../src/lib/item";
import { mapState, PROCESSED_TIMEOUT_MS, waitUntilProcessed } from "./stack";

test("a queued tag change shows in the write log as a dry run, before and after", async ({
  page,
  request,
}) => {
  // The first spec on a fresh stack waits for the worker to process the library.
  test.setTimeout(PROCESSED_TIMEOUT_MS + 60_000);
  await waitUntilProcessed(request);
  await page.goto("/");
  await expect.poll(async () => (await mapState(page)).visible.length).toBeGreaterThan(0);
  let target: ItemDetails | null = null;
  for (const id of (await mapState(page)).visible) {
    const item = (await (await request.get(`/api/items/${String(id)}`)).json()) as ItemDetails;
    if (item.path === "metadata/keywords.jpg") {
      target = item;
      break;
    }
  }
  if (target === null) throw new Error("metadata/keywords.jpg isn't on the map");
  const id = target.id;
  const change = (action: "add" | "remove") =>
    request.post("/api/tags", { data: { item_ids: [id], tag: "e2e log", action } });
  // Before is as the file has it, in its own order.
  const sorted = (text: string) => text.split(", ").sort();
  const after = [...target.tags, "e2e log"]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
    .join(", ");

  expect((await change("add")).ok()).toBe(true);
  try {
    // Reached from the Activity page's metadata-writes row.
    await page.goto("/activity");
    await page.getByRole("link", { name: "Write log" }).click();
    await expect(page.getByRole("heading", { name: "Write log" })).toBeVisible();
    await expect(page.getByRole("note")).toContainText("Dry run");
    await page
      .getByRole("group", { name: "Status" })
      .getByRole("button", { name: "Dry run" })
      .click();

    // The worker logs it as a dry run soon after it's queued.
    const entry = page.getByRole("listitem").filter({ hasText: after }).first();
    await expect(async () => {
      await page.reload();
      await page
        .getByRole("group", { name: "Status" })
        .getByRole("button", { name: "Dry run" })
        .click();
      await expect(entry).toBeVisible({ timeout: 1000 });
    }).toPass({ timeout: 30_000 });
    await expect(entry).toContainText("metadata/keywords.jpg");
    await expect(entry).toContainText("Dry run");
    expect(sorted(await entry.getByTitle("Before").innerText())).toEqual([...target.tags].sort());
    await expect(entry.getByTitle("After")).toHaveText(after);
  } finally {
    expect((await change("remove")).ok()).toBe(true);
  }
});
