import { describe, expect, it, vi } from "vitest";

import { ApiError, getHealth, getJson, setPaused } from "../src/lib/api";

function fakeFetch(status: number, body: unknown) {
  return vi.fn<typeof fetch>(() => Promise.resolve(Response.json(body, { status })));
}

describe("getJson", () => {
  it("requests the path under /api and returns the body", async () => {
    const fetchFn = fakeFetch(200, { status: "ok" });

    await expect(getHealth(fetchFn)).resolves.toEqual({ status: "ok" });
    expect(fetchFn).toHaveBeenCalledWith("/api/health", expect.anything());
  });

  it("throws ApiError with the status when the response isn't OK", async () => {
    const error = await getJson("/missing", fakeFetch(404, {})).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ method: "GET", path: "/missing", status: 404 });
  });
});

describe("setPaused", () => {
  it.each([
    [true, null, "/api/worker/pause"],
    [false, null, "/api/worker/resume"],
    [true, "llm_tag", "/api/worker/stages/llm_tag/pause"],
    [false, "scan", "/api/worker/stages/scan/resume"],
  ] as const)("paused=%s for %s posts to %s", async (paused, stage, url) => {
    const fetchFn = fakeFetch(200, { worker: {}, stages: [] });

    await setPaused(paused, stage, fetchFn);
    expect(fetchFn).toHaveBeenCalledWith(url, expect.objectContaining({ method: "POST" }));
  });
});
