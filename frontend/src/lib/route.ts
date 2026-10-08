/** The app's pages, at plain paths (`/activity`). The API answers any path outside
 * `/api` with `index.html`, and links within the app change the page without a
 * reload, through the History API. */

export type Route = "home" | "activity" | "missing";

export const ROUTE_HREF = {
  home: "/",
  activity: "/activity",
} as const satisfies Partial<Record<Route, string>>;

export function parseRoute(pathname: string): Route {
  switch (pathname.replace(/\/+$/, "")) {
    case "":
      return "home";
    case "/activity":
      return "activity";
    default:
      return "missing";
  }
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
