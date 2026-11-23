import { expect, test } from "@playwright/test";

import { matchCount, type SearchResults } from "../src/lib/search";
import { highlightedIds, mapState, waitUntilProcessed } from "./stack";

/* The tier-1 fixtures were taken from July 2012 to November 2024, most in 2024, and
 * some have no date. The stack embeds with FakeClip, so about half match a search. */

test("narrowing the dates dims what's outside them, and stacks with search", async ({
  page,
  request,
  hasTouch,
}) => {
  const done = await waitUntilProcessed(request);
  await page.goto("/");
  await expect.poll(async () => (await mapState(page)).version).toBe(done.layout_version);
  const everything = (await mapState(page)).visible;
  expect(await highlightedIds(page)).toEqual([]);

  // A tap on the track, two fifths along (about 2017), moves the nearer end, the
  // start, there: 2012's item and the undated ones fall outside.
  const track = page.getByRole("group", { name: "Dates" });
  const box = await track.boundingBox();
  if (box === null) throw new Error("No date slider");
  const at = { x: box.width * 0.4, y: box.height / 2 };
  if (hasTouch) await track.tap({ position: at });
  else await track.click({ position: at });

  await expect(page.getByRole("slider", { name: "From" })).not.toHaveAttribute(
    "aria-valuetext",
    "Jul 2012",
  );
  const inRange = await highlightedIds(page);
  expect(inRange.length).toBeGreaterThan(0);
  expect(inRange.length).toBeLessThan(everything.length);
  await expect(page.getByText(`${String(inRange.length)} items`)).toBeVisible();

  // With a search, only matches in range are highlighted, best first.
  const response = await request.get("/api/search?q=the%20beach");
  const matches = ((await response.json()) as SearchResults).matches.map((m) => m.id);
  const searchBox = page.getByRole("searchbox", { name: "Search the library" });
  await searchBox.fill("the beach");
  await searchBox.press("Enter");
  const expected = matches.filter((id) => inRange.includes(id));
  await expect(page.getByRole("status")).toContainText(matchCount(expected.length, "en-US"));
  expect(await highlightedIds(page)).toEqual(expected);

  // Back to every date: the search's matches alone.
  await page.getByRole("button", { name: "All dates" }).click();
  await expect.poll(() => highlightedIds(page)).toEqual(matches);
});
