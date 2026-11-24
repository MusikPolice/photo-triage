import { describe, expect, it, vi } from "vitest";

import {
  fieldChanges,
  formatValue,
  getWriteLog,
  statusLabel,
  WRITE_PAGE_SIZE,
  type LoggedWrite,
} from "../src/lib/writes";

function write(overrides: Partial<LoggedWrite> = {}): LoggedWrite {
  return {
    id: 1,
    item_id: 2,
    path: "metadata/keywords.jpg",
    fields: { tags: ["beach", "dog"] },
    before: { tags: ["beach"] },
    after: { tags: ["beach", "dog"] },
    status: "dry_run",
    actor: "ada",
    created_at: "2026-11-24T12:00:00Z",
    applied_at: "2026-11-24T12:00:05Z",
    ...overrides,
  };
}

describe("fieldChanges", () => {
  it("shows each field before and after", () => {
    expect(fieldChanges(write())).toEqual([
      { label: "Tags", before: "beach", after: "beach, dog" },
    ]);
  });

  it("has no before for a write not yet read from the file", () => {
    const queued = write({ status: "queued", before: null, after: null, applied_at: null });
    expect(fieldChanges(queued)).toEqual([{ label: "Tags", before: null, after: "beach, dog" }]);
  });

  it("shows a field the file already had as unchanged", () => {
    const same = write({
      fields: { tags: ["dog"], reviewed: ["duplicates"] },
      before: { tags: [] },
      after: { tags: ["dog"] },
    });
    expect(fieldChanges(same)).toEqual([
      { label: "Tags", before: "none", after: "dog" },
      { label: "Reviewed", before: "duplicates", after: "duplicates" },
    ]);
  });
});

describe("formatValue", () => {
  it.each([
    [["a", "b"], "a, b"],
    [[], "none"],
    [null, "none"],
    ["x", "x"],
    [{ a: 1 }, '{"a":1}'],
  ])("%j is %j", (value, shown) => {
    expect(formatValue(value)).toBe(shown);
  });
});

it("labels each status", () => {
  expect(["queued", "applied", "dry_run", "error"].map((s) => statusLabel(s as never))).toEqual([
    "Queued",
    "Applied",
    "Dry run",
    "Error",
  ]);
});

describe("getWriteLog", () => {
  const page = { items: [], total: 0, writes_enabled: false };

  it("pages the whole log", async () => {
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(page)));
    expect(await getWriteLog(null, 100, undefined, fetchFn)).toEqual(page);
    expect(fetchFn).toHaveBeenCalledWith(
      `/api/metadata-writes?limit=${String(WRITE_PAGE_SIZE)}&offset=100`,
      expect.anything(),
    );
  });

  it("filters by status", async () => {
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(page)));
    await getWriteLog("dry_run", 0, 10, fetchFn);
    expect(fetchFn).toHaveBeenCalledWith(
      "/api/metadata-writes?limit=10&offset=0&status=dry_run",
      expect.anything(),
    );
  });
});
