import { expect, test } from "@playwright/test";

import type { SearchResults } from "../src/lib/search";
import { highlightedIds, mapState, pagePosition, waitUntilProcessed } from "./stack";

/* The stack embeds with FakeClip (`WORKER_FAKE_CLIP`), whose vectors are random, and
 * whose floor of 0 lets about half the items match any query. */
const QUERY = "the beach";

test("a search highlights its matches in place, and clearing it restores the map", async ({
  page,
  request,
}) => {
  const done = await waitUntilProcessed(request);
  const response = await request.get(`/api/search?q=${encodeURIComponent(QUERY)}`);
  expect(response.ok()).toBe(true);
  const expected = ((await response.json()) as SearchResults).matches.map((m) => m.id);
  expect(expected.length).toBeGreaterThan(0);

  await page.goto("/");
  await expect.poll(async () => (await mapState(page)).version).toBe(done.layout_version);
  const before = await mapState(page);

  const box = page.getByRole("searchbox", { name: "Search the library" });
  await box.fill(QUERY);
  await box.press("Enter");

  await expect(page.getByRole("status")).toContainText(`${String(expected.length)} matches`);
  expect(await highlightedIds(page)).toEqual(expected);
  // In place: the map hasn't moved.
  expect(await mapState(page)).toEqual(before);

  // Flying to the best match brings it to the centre.
  await page.getByRole("button", { name: "Fly to best match" }).click();
  const canvas = await page.locator(".map canvas").boundingBox();
  if (canvas === null) throw new Error("No map canvas");
  const [best] = expected;
  if (best === undefined) throw new Error("No best match");
  await expect
    .poll(async () => {
      const at = await pagePosition(page, best);
      if (at === null) return Infinity;
      return Math.hypot(
        at[0] - (canvas.x + canvas.width / 2),
        at[1] - (canvas.y + canvas.height / 2),
      );
    })
    .toBeLessThan(2);

  await page.getByRole("button", { name: "Clear" }).click();

  await expect(box).toHaveValue("");
  await expect(page.getByRole("status")).toHaveCount(0);
  expect(await highlightedIds(page)).toEqual([]);
});
