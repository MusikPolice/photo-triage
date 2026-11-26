import { describe, expect, it, vi } from "vitest";

import {
  allSelected,
  formatDay,
  getTrash,
  movedSummary,
  purgeWording,
  restoreItems,
  toggled,
  toggledAll,
  trashItems,
  TRASH_PAGE_SIZE,
  within,
} from "../src/lib/trash";

const NOW = new Date("2026-11-24T12:00:00Z");
const HOUR_MS = 60 * 60 * 1000;

function inHours(hours: number): string {
  return new Date(NOW.getTime() + hours * HOUR_MS).toISOString();
}

describe("purgeWording", () => {
  it.each([
    [12 * 24, "purged in 12 days"],
    [30 * 24 - 0.01, "purged in 30 days"],
    [36, "purged in 2 days"],
    [30, "purged in 1 day"],
    [24, "purged in 1 day"],
    [23, "purged within a day"],
    [0.01, "purged within a day"],
    [0, "purged soon"],
    [-48, "purged soon"],
  ])("%d hours left is %j", (hours, wording) => {
    expect(purgeWording(inHours(hours), NOW)).toBe(wording);
  });

  it("is empty for a date it can't read", () => {
    expect(purgeWording("not a date", NOW)).toBe("");
  });
});

describe("formatDay", () => {
  it("shows the date", () => {
    expect(formatDay("2026-11-03T12:00:00Z", { locale: "en-GB" })).toBe("3 Nov 2026");
  });

  it("passes through what it can't read", () => {
    expect(formatDay("soon")).toBe("soon");
  });
});

describe("selection", () => {
  it("toggles one item in and out", () => {
    const one = toggled(new Set(), 3);
    expect([...one]).toEqual([3]);
    expect([...toggled(one, 3)]).toEqual([]);
  });

  it("doesn't change the selection it was given", () => {
    const before = new Set([1]);
    toggled(before, 2);
    expect([...before]).toEqual([1]);
  });

  it("knows when all are selected", () => {
    expect(allSelected(new Set([1, 2]), [1, 2])).toBe(true);
    expect(allSelected(new Set([1, 2, 9]), [1, 2])).toBe(true);
    expect(allSelected(new Set([1]), [1, 2])).toBe(false);
    expect(allSelected(new Set(), [])).toBe(false);
  });

  it("selects all, or none once all are", () => {
    expect([...toggledAll(new Set([2]), [1, 2, 3])]).toEqual([1, 2, 3]);
    expect([...toggledAll(new Set([1, 2, 3]), [1, 2, 3])]).toEqual([]);
  });

  it("drops items no longer listed", () => {
    expect([...within(new Set([1, 2, 3]), [3, 4, 1])]).toEqual([3, 1]);
  });
});

describe("movedSummary", () => {
  it("counts what moved", () => {
    expect(movedSummary("Restored", { moved: [1, 2, 3], skipped: [] })).toBe("Restored 3 items.");
    expect(movedSummary("Restored", { moved: [1], skipped: [] })).toBe("Restored 1 item.");
  });

  it("says why the rest didn't, each reason once", () => {
    const result = {
      moved: [1],
      skipped: [
        { id: 2, reason: "It isn't in the trash" },
        { id: 3, reason: "It isn't in the trash" },
      ],
    };
    expect(movedSummary("Restored", result)).toBe(
      "Restored 1 item. 2 items couldn't be: It isn't in the trash.",
    );
  });

  it("leaves out the count when nothing moved", () => {
    expect(movedSummary("Restored", { moved: [], skipped: [{ id: 2, reason: "Gone" }] })).toBe(
      "1 item couldn't be: Gone.",
    );
  });
});

describe("the API", () => {
  const moved = { moved: [4], skipped: [] };

  it.each([
    ["trash", trashItems, "/api/trash"],
    ["restore", restoreItems, "/api/trash/restore"],
  ] as const)("%s posts the items as JSON", async (_, call, url) => {
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(moved)));

    expect(await call([4], fetchFn)).toEqual(moved);

    const [called, init] = fetchFn.mock.calls[0] ?? [];
    expect(called).toBe(url);
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init?.body as string)).toEqual({ item_ids: [4] });
  });

  it("pages through the trash", async () => {
    const page = { items: [], total: 0 };
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(page)));

    expect(await getTrash(200, undefined, fetchFn)).toEqual(page);
    expect(fetchFn).toHaveBeenCalledWith(
      `/api/trash?limit=${String(TRASH_PAGE_SIZE)}&offset=200`,
      expect.anything(),
    );
  });
});
