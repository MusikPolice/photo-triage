import { describe, expect, it, vi } from "vitest";

import {
  decideGroup,
  defaultKeep,
  getNextGroup,
  memberFacts,
  reviewedSummary,
  reviewerToken,
  trashCount,
  type Member,
} from "../src/lib/duplicates";

function member(id: number, rank: number, overrides: Partial<Member> = {}): Member {
  return {
    id,
    rank,
    media_type: "photo",
    path: `a/${String(id)}.jpg`,
    width: 4032,
    height: 3024,
    file_size_bytes: 3_100_000,
    taken_at_local: "2019-04-15T10:20:30",
    thumbnail_url: `/api/items/${String(id)}/thumbnail?size=400`,
    view_url: `/api/items/${String(id)}/view`,
    ...overrides,
  };
}

describe("reviewerToken", () => {
  it("keeps one token for the tab", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    };
    const first = reviewerToken(storage, () => "token-0001");
    expect(first).toBe("token-0001");
    expect(reviewerToken(storage, () => "token-0002")).toBe("token-0001");
  });

  it("makes a new one for a stored value that isn't a token", () => {
    const storage = { getItem: () => "bad token!", setItem: vi.fn() };
    expect(reviewerToken(storage, () => "token-0003")).toBe("token-0003");
  });

  it("works without storage, or with storage that throws", () => {
    expect(reviewerToken(null, () => "token-0004")).toBe("token-0004");
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: vi.fn(),
    };
    expect(reviewerToken(broken, () => "token-0005")).toBe("token-0005");
  });
});

it("counts the groups reviewed", () => {
  expect(reviewedSummary(128, 140)).toBe("12 of 140 groups reviewed");
  expect(reviewedSummary(1, 1)).toBe("0 of 1 group reviewed");
});

describe("members", () => {
  const members = [member(1, 0), member(2, 1), member(3, 2)];

  it("keeps the recommended one to start with", () => {
    expect([...defaultKeep([member(2, 1), member(1, 0)])]).toEqual([1]);
    expect([...defaultKeep([])]).toEqual([]);
  });

  it("counts what keeping the selection would trash", () => {
    expect(trashCount(new Set([1]), members)).toBe(2);
    expect(trashCount(new Set([1, 2, 3]), members)).toBe(0);
  });

  it("says each one's size", () => {
    expect(memberFacts(member(1, 0))).toEqual(["4,032 × 3,024", "3.1 MB"]);
    expect(memberFacts(member(1, 0, { width: null }))).toEqual(["3.1 MB"]);
  });
});

describe("the API", () => {
  it("asks for the next group as this reviewer", async () => {
    const answer = { group: null, pending: 0, total: 0 };
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(answer)));
    expect(await getNextGroup("token-0001", fetchFn)).toEqual(answer);
    const [url, init] = fetchFn.mock.calls[0] ?? [];
    expect(url).toBe("/api/duplicates/next");
    expect(JSON.parse(init?.body as string)).toEqual({ reviewer: "token-0001" });
  });

  it("sends the decision", async () => {
    const answer = { moved: [2], skipped: [] };
    const fetchFn = vi.fn<typeof fetch>(() => Promise.resolve(Response.json(answer)));
    expect(await decideGroup(7, "token-0001", "keep_selected", [1], fetchFn)).toEqual(answer);
    const [url, init] = fetchFn.mock.calls[0] ?? [];
    expect(url).toBe("/api/duplicates/7/decide");
    expect(JSON.parse(init?.body as string)).toEqual({
      reviewer: "token-0001",
      action: "keep_selected",
      keep: [1],
    });
  });
});

it("releases a group with a beacon", async () => {
  const beacon = vi.fn<(url: string, data: Blob) => boolean>(() => true);
  const { releaseGroup } = await import("../src/lib/duplicates");
  releaseGroup(7, "token-0001", beacon);
  const [url, data] = beacon.mock.calls[0] ?? [];
  expect(url).toBe("/api/duplicates/7/release");
  expect(data?.type).toBe("application/json");
  expect(JSON.parse((await data?.text()) ?? "")).toEqual({ reviewer: "token-0001" });
});
