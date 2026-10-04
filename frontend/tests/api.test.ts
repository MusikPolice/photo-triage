import { describe, expect, it, vi } from "vitest";

import { ApiError, getHealth, getJson } from "../src/lib/api";

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
    expect(error).toMatchObject({ path: "/missing", status: 404 });
  });
});
