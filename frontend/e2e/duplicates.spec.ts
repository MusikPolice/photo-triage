import { expect, test } from "@playwright/test";

import type { components } from "../src/lib/api-types";
import { waitUntilProcessed } from "./stack";

/** The stack has two pairs of identical copies (scripts/e2e.sh), so two groups: each
 * project resolves one, and restores what it trashed for the specs after it. */
test("a group resolved with Keep selected sends the others to the trash", async ({
  page,
  request,
  isMobile,
}) => {
  await waitUntilProcessed(request);
  await page.goto("/");
  await page.getByRole("link", { name: "Duplicates" }).click();
  await expect(page.getByRole("heading", { name: "Duplicates" })).toBeVisible();

  const members = page.getByRole("list", { name: "Members" }).getByRole("listitem");
  await expect(members).toHaveCount(2);
  await expect(page.getByText(/of 2 groups reviewed/)).toBeVisible();
  await expect(members.first().getByText("Recommended")).toBeVisible();
  await expect(members.first().getByRole("checkbox", { name: "Keep" })).toBeChecked();
  await expect(members.nth(1).getByRole("checkbox", { name: "Keep" })).not.toBeChecked();

  if (isMobile) {
    // One member at a time: a swipe brings the next into view.
    await expect(page.getByText("1 of 2", { exact: true })).toBeVisible();
    const box = await page.getByRole("list", { name: "Members" }).boundingBox();
    if (box === null) throw new Error("No members strip");
    // A finger dragged from right to left across the strip.
    const cdp = await page.context().newCDPSession(page);
    const y = Math.round(box.y + box.height / 3);
    const from = Math.round(box.x + box.width * 0.85);
    const to = Math.round(box.x + box.width * 0.15);
    const touch = (type: "touchStart" | "touchMove" | "touchEnd", x: number) =>
      cdp.send("Input.dispatchTouchEvent", {
        type,
        touchPoints: type === "touchEnd" ? [] : [{ x, y }],
      });
    await touch("touchStart", from);
    for (let step = 1; step <= 10; step++) {
      await touch("touchMove", from + ((to - from) * step) / 10);
    }
    await touch("touchEnd", to);
    await expect(page.getByText("2 of 2", { exact: true })).toBeVisible();
    await expect(members.nth(1)).toBeInViewport();
  }

  await page.getByRole("button", { name: "Keep selected, trash 1" }).click();

  await expect(page.getByRole("status")).toHaveText("Moved 1 to the trash.");
  const trash = (await (
    await request.get("/api/trash")
  ).json()) as components["schemas"]["TrashPage"];
  expect(trash.items).toHaveLength(1);
  // The page has moved on to the other group: leaving lets it go, for the next project.
  await page.getByRole("link", { name: "photo-triage" }).click();
  await expect(page.getByRole("heading", { name: "Duplicates" })).toBeHidden();
  const restored = await request.post("/api/trash/restore", {
    data: { item_ids: trash.items.map((i) => i.id) },
  });
  expect(restored.ok()).toBe(true);
});
