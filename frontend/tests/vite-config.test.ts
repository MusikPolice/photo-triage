import { describe, expect, it } from "vitest";

import { apiTarget } from "../vite.config";

describe("the /api proxy", () => {
  it("targets APP_PORT", () => {
    expect(apiTarget({ APP_PORT: "9123" })).toBe("http://127.0.0.1:9123");
  });

  it("defaults to the API's default port", () => {
    expect(apiTarget({})).toBe("http://127.0.0.1:8000");
  });
});
