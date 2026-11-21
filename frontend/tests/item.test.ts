import { describe, expect, it, vi } from "vitest";

import {
  fileName,
  formatBytes,
  formatDimensions,
  formatDuration,
  formatTakenAt,
  getItem,
  itemFromSearch,
  viewUrl,
  withItem,
} from "../src/lib/item";

const EN = { locale: "en-GB" };

describe("getItem", () => {
  it("reads the item's details", async () => {
    const details = { id: 7, path: "a.jpg" };
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(details)));

    expect(await getItem(7, fetchFn)).toEqual(details);
    expect(fetchFn).toHaveBeenCalledWith("/api/items/7", expect.anything());
  });

  it("fails on an item that isn't there", async () => {
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(new Response("", { status: 404 })));
    await expect(getItem(7, fetchFn)).rejects.toThrow("HTTP 404");
  });

  it("shows the item from its view URL", () => {
    expect(viewUrl(7)).toBe("/api/items/7/view");
  });
});

describe("the lightbox's URL", () => {
  it.each([
    ["?item=12", 12],
    ["?colour=year&item=3", 3],
    ["", null],
    ["?item=", null],
    ["?item=0", null],
    ["?item=-4", null],
    ["?item=1.5", null],
    ["?item=12abc", null],
    ["?item=99999999999999999999", null],
  ])("reads %j as %j", (search, id) => {
    expect(itemFromSearch(search)).toBe(id);
  });

  it("opens an item, keeping other parameters", () => {
    expect(withItem("", 12)).toBe("?item=12");
    expect(withItem("?colour=year", 12)).toBe("?colour=year&item=12");
    expect(withItem("?item=3", 12)).toBe("?item=12");
  });

  it("closes, leaving no empty query", () => {
    expect(withItem("?item=12", null)).toBe("");
    expect(withItem("?colour=year&item=12", null)).toBe("?colour=year");
  });
});

describe("formatting", () => {
  it("shows a capture time as the camera's wall-clock time", () => {
    expect(formatTakenAt("2019-04-15T10:20:30", EN)).toBe("15 April 2019 at 10:20");
  });

  it("shows anything unreadable as it is", () => {
    expect(formatTakenAt("not a date", EN)).toBe("not a date");
  });

  it.each([
    [7.4, "0:07"],
    [205, "3:25"],
    [3723, "1:02:03"],
    [59.6, "1:00"],
  ])("shows %f s as %s", (seconds, shown) => {
    expect(formatDuration(seconds)).toBe(shown);
  });

  it.each([
    [512, "512 B"],
    [820_000, "820 kB"],
    [3_140_000, "3.1 MB"],
    [1_234_000_000, "1.2 GB"],
  ])("shows %i bytes as %s", (bytes, shown) => {
    expect(formatBytes(bytes, EN)).toBe(shown);
  });

  it("shows dimensions, if known", () => {
    expect(formatDimensions(4032, 3024, EN)).toBe("4,032 × 3,024");
    expect(formatDimensions(null, 3024, EN)).toBeNull();
  });

  it("names the file from its path", () => {
    expect(fileName("2019/April/2019-04-15_10-20-30-beach.jpg")).toBe(
      "2019-04-15_10-20-30-beach.jpg",
    );
    expect(fileName("top.jpg")).toBe("top.jpg");
  });
});
