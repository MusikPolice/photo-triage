import { describe, expect, it } from "vitest";

import { inAppPath, parseRoute, ROUTE_HREF, type LinkClick } from "../src/lib/route";

const ORIGIN = "http://nas.local:8000";

it.each([
  ["/", "home"],
  ["", "home"],
  ["/activity", "activity"],
  ["/activity/", "activity"],
  ["/nowhere", "missing"],
])("%j is %s", (pathname, route) => {
  expect(parseRoute(pathname)).toBe(route);
});

it("parses its own links", () => {
  expect(parseRoute(ROUTE_HREF.activity)).toBe("activity");
  expect(parseRoute(ROUTE_HREF.home)).toBe("home");
});

describe("inAppPath", () => {
  function click(href: string, fields: Partial<LinkClick> = {}, attrs: string[] = []): LinkClick {
    return {
      button: 0,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      defaultPrevented: false,
      link: { href, target: "", hasAttribute: (name) => attrs.includes(name) },
      ...fields,
    };
  }

  it("handles a plain click on a link within the app", () => {
    expect(inAppPath(click(`${ORIGIN}/activity`), ORIGIN)).toBe("/activity");
    expect(inAppPath(click(`${ORIGIN}/?q=dog#top`), ORIGIN)).toBe("/?q=dog#top");
  });

  it.each([
    ["another site", click("https://example.com/activity")],
    ["the API", click(`${ORIGIN}/api/docs`)],
    ["a middle click", click(`${ORIGIN}/activity`, { button: 1 })],
    ["a ctrl-click", click(`${ORIGIN}/activity`, { ctrlKey: true })],
    ["a handled click", click(`${ORIGIN}/activity`, { defaultPrevented: true })],
    ["a download", click(`${ORIGIN}/activity`, {}, ["download"])],
  ])("leaves %s to the browser", (_, linkClick) => {
    expect(inAppPath(linkClick, ORIGIN)).toBeNull();
  });

  it("leaves links that open elsewhere to the browser", () => {
    const newTab = click(`${ORIGIN}/activity`);
    newTab.link.target = "_blank";
    expect(inAppPath(newTab, ORIGIN)).toBeNull();
  });
});
