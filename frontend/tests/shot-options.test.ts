import { describe, expect, it } from "vitest";

import { fileName, nameFromPath, parseShotArgs, UsageError } from "../scripts/shot-options.ts";

describe("parseShotArgs", () => {
  it("takes both viewports of the scratch stack by default", () => {
    expect(parseShotArgs(["/activity"])).toEqual({
      path: "/activity",
      name: "activity",
      viewports: ["desktop", "phone"],
      clicks: [],
      wait: null,
      noop: 0,
      base: null,
    });
  });

  it("reads every option, keeping clicks in order", () => {
    const options = parseShotArgs([
      "/activity",
      "--noop=200",
      "--wait",
      "h2",
      "--click",
      "text=Pause all",
      "--click",
      "text=Resume all",
      "--viewport",
      "phone",
      "--name",
      "resumed",
    ]);
    expect(options).toMatchObject({
      noop: 200,
      wait: "h2",
      clicks: ["text=Pause all", "text=Resume all"],
      viewports: ["phone"],
      name: "resumed",
    });
  });

  it("drops a trailing slash from --base", () => {
    expect(parseShotArgs(["/", "--base", "http://localhost:5173/"]).base).toBe(
      "http://localhost:5173",
    );
  });

  it.each([
    [[], "Give the page's path"],
    [["activity"], 'starts with "/"'],
    [["/a", "/b"], "Only one path"],
    [["/a", "--viewport", "tablet"], "desktop or phone"],
    [["/a", "--noop", "0"], "number of jobs"],
    [["/a", "--noop", "lots"], "number of jobs"],
    [["/a", "--base", "localhost:5173"], "is a URL"],
    [["/a", "--base", "http://localhost:5173", "--noop", "5"], "can't be used with --base"],
    [["/a", "--name", "../x"], "--name uses"],
    [["/a", "--bogus"], "Unknown option"],
  ])("rejects %j", (argv, message) => {
    expect(() => parseShotArgs(argv)).toThrow(UsageError);
    expect(() => parseShotArgs(argv)).toThrow(message);
  });
});

describe("file names", () => {
  it.each([
    ["/", "home"],
    ["/activity", "activity"],
    ["/activity/", "activity"],
    ["/map/some/where?zoom=3#x", "map-some-where"],
    ["/a b", "a_b"],
  ])("names %s %s", (path, name) => {
    expect(nameFromPath(path)).toBe(name);
  });

  it("adds the viewport", () => {
    expect(fileName("activity", "phone")).toBe("activity-phone.png");
  });
});
