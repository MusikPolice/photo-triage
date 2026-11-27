/** The app's pages, at plain paths (`/activity`). The API answers any path outside
 * `/api` with `index.html`, and links within the app change the page without a
 * reload, through the History API. */

/** Each page's path. Alphabetical after `home`, so PRs adding different pages
 * edit different lines. */
export const ROUTE_HREF = {
  home: "/",
  activity: "/activity",
  duplicates: "/duplicates",
  trash: "/trash",
  writes: "/writes",
} as const;

/** The app's pages, and `missing` for any other path. */
export type Route = keyof typeof ROUTE_HREF | "missing";

function withoutTrailingSlash(pathname: string): string {
  return pathname.replace(/\/+$/, "");
}

const ROUTE_BY_PATH: ReadonlyMap<string, Route> = new Map(
  Object.entries(ROUTE_HREF).map(([route, href]) => [withoutTrailingSlash(href), route as Route]),
);

export function parseRoute(pathname: string): Route {
  return ROUTE_BY_PATH.get(withoutTrailingSlash(pathname)) ?? "missing";
}

/** What's needed of a click on a link. */
export interface LinkClick {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  defaultPrevented: boolean;
  link: { href: string; target: string; hasAttribute(name: string): boolean };
}

/** The path to show for a click on a link, without a reload, or null if the
 * browser should follow it: another site, the API, a new tab or a download. */
export function inAppPath(click: LinkClick, origin: string): string | null {
  const { link } = click;
  const modified = click.metaKey || click.ctrlKey || click.shiftKey || click.altKey;
  if (click.button !== 0 || modified || click.defaultPrevented) return null;
  if ((link.target !== "" && link.target !== "_self") || link.hasAttribute("download")) return null;
  const url = new URL(link.href, origin);
  if (url.origin !== origin || url.pathname === "/api" || url.pathname.startsWith("/api/")) {
    return null;
  }
  return url.pathname + url.search + url.hash;
}
