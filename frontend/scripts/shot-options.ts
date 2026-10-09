/** `just shot`'s command line, and where its screenshots go (dev-environment §5). */

import { parseArgs } from "node:util";

export const VIEWPORTS = {
  desktop: { viewport: { width: 1280, height: 800 } },
  // A phone at 390 px, as Chromium emulates one: touch, mobile layout, 2x pixels.
  phone: {
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  },
} as const;

export type Viewport = keyof typeof VIEWPORTS;

export const DEFAULT_BASE = "http://localhost:5173";

export const USAGE = `Usage: just shot PATH [options]

Screenshots of PATH (e.g. /activity) at desktop and phone widths, saved to
.screenshots/. Without --base it uses a running \`just preview\`, or starts a
scratch stack for this shot alone and stops it afterwards.

  --noop N             queue N noop jobs first (scratch stack only)
  --wait SELECTOR      wait for an element before shooting
  --click SELECTOR     click an element first; repeat to click several, in order
  --viewport desktop|phone
                       just one of the two
  --name NAME          file name stem (default: from PATH, e.g. "activity")
  --base URL           a server that's already running, e.g. your \`just dev\`
                       (${DEFAULT_BASE}). Its screenshots go to .screenshots/local/,
                       which tracker.py won't publish.`;

export interface ShotOptions {
  path: string;
  name: string;
  viewports: Viewport[];
  clicks: string[];
  wait: string | null;
  noop: number;
  base: string | null;
}

export class UsageError extends Error {}

/** "/" is "home"; "/activity/today?x=1" is "activity-today". */
export function nameFromPath(path: string): string {
  const stem = path
    .replace(/[?#].*$/, "")
    .split("/")
    .filter((part) => part !== "")
    .join("-")
    .replace(/[^A-Za-z0-9_-]/g, "_");
  return stem === "" ? "home" : stem;
}

export function fileName(name: string, viewport: Viewport): string {
  return `${name}-${viewport}.png`;
}

export function parseShotArgs(argv: string[]): ShotOptions {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      allowPositionals: true,
      options: {
        noop: { type: "string" },
        wait: { type: "string" },
        click: { type: "string", multiple: true },
        viewport: { type: "string" },
        name: { type: "string" },
        base: { type: "string" },
      },
    });
  } catch (e) {
    throw new UsageError(e instanceof Error ? e.message : String(e));
  }
  const { values, positionals } = parsed;

  const [path, ...extra] = positionals;
  if (path === undefined) throw new UsageError("Give the page's path, e.g. /activity");
  if (extra.length > 0) throw new UsageError(`Only one path, not also ${extra.join(" ")}`);
  if (!path.startsWith("/")) throw new UsageError(`The path starts with "/", e.g. /${path}`);

  let viewports: Viewport[] = ["desktop", "phone"];
  if (values.viewport !== undefined) {
    if (values.viewport !== "desktop" && values.viewport !== "phone") {
      throw new UsageError(`--viewport is desktop or phone, not ${values.viewport}`);
    }
    viewports = [values.viewport];
  }

  let noop = 0;
  if (values.noop !== undefined) {
    noop = Number(values.noop);
    if (!Number.isInteger(noop) || noop < 1) {
      throw new UsageError(`--noop takes a number of jobs, not ${values.noop}`);
    }
  }

  const base = values.base ?? null;
  if (base !== null) {
    if (!/^https?:\/\//.test(base)) throw new UsageError(`--base is a URL, not ${base}`);
    if (noop > 0) {
      throw new UsageError("--noop needs the scratch stack; it can't be used with --base");
    }
  }

  const name = values.name ?? nameFromPath(path);
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    throw new UsageError(`--name uses letters, digits, "-" and "_", not ${name}`);
  }

  return {
    path,
    name,
    viewports,
    clicks: values.click ?? [],
    wait: values.wait ?? null,
    noop,
    base: base?.replace(/\/+$/, "") ?? null,
  };
}
