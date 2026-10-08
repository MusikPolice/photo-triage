/** `just shot` (dev-environment §5): screenshots of a page at desktop and phone
 * widths, for looking at a layout without a browser at hand. Run with Node, which
 * strips the types. See `USAGE` in shot-options.ts. */

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

import { chromium, type Browser } from "@playwright/test";

import {
  fileName,
  parseShotArgs,
  USAGE,
  UsageError,
  VIEWPORTS,
  type ShotOptions,
} from "./shot-options.ts";

const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
const SHOTS_DIR = join(REPO_ROOT, ".screenshots");
/** Written by `just preview` while it runs. */
const PREVIEW_STATE = join(SHOTS_DIR, "preview.json");
const STACK_READY_S = 90;
const STOP_S = 30;
const SETTLE_MS = 500;

/** What scripts/scratch_stack.sh writes with --state. */
interface StackState {
  url: string;
  env: Record<string, string>;
}

class ShotError extends Error {}

async function answers(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return response.ok;
  } catch {
    return false;
  }
}

function readState(path: string): StackState | null {
  try {
    return JSON.parse(readFileSync(path, "utf8")) as StackState;
  } catch {
    return null;
  }
}

/** A scratch stack started for this shot alone, stopped by `stop()`. */
class OwnStack {
  private readonly child: ChildProcess;
  private readonly statePath: string;
  private readonly output: string[] = [];
  private exited = false;
  private stopping: Promise<void> | null = null;

  // Plain fields, not parameter properties, which Node can't strip.
  private constructor(child: ChildProcess, statePath: string) {
    this.child = child;
    this.statePath = statePath;
    const keep = (chunk: Buffer) => {
      this.output.push(...chunk.toString().split("\n"));
      this.output.splice(0, Math.max(0, this.output.length - 40));
    };
    child.stdout?.on("data", keep);
    child.stderr?.on("data", keep);
    child.on("exit", () => (this.exited = true));
  }

  static start(): OwnStack {
    const statePath = join(SHOTS_DIR, `.stack-${String(process.pid)}.json`);
    // Its own process group, so a Ctrl-C here reaches it only through stop().
    const child = spawn(join(REPO_ROOT, "scripts/scratch_stack.sh"), ["--state", statePath], {
      cwd: REPO_ROOT,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    return new OwnStack(child, statePath);
  }

  async ready(): Promise<StackState> {
    for (let i = 0; i < STACK_READY_S * 4; i++) {
      const state = readState(this.statePath);
      if (state !== null) return state;
      if (this.exited) break;
      await sleep(250);
    }
    throw new ShotError(
      `The scratch stack didn't start within ${String(STACK_READY_S)} s. Its last output:\n` +
        this.output.filter((line) => line !== "").join("\n"),
    );
  }

  /** Stops it once, however often it's called: the script's cleanup must not be
   * interrupted by a second signal. */
  stop(): Promise<void> {
    this.stopping ??= this.terminate();
    return this.stopping;
  }

  private async terminate(): Promise<void> {
    if (this.exited || this.child.pid === undefined) return;
    const exited = new Promise((resolve) => this.child.once("exit", resolve));
    // The script stops dev.sh, which stops each server once (scripts/dev.sh).
    this.child.kill("SIGTERM");
    const timedOut = await Promise.race([
      exited.then(() => false),
      sleep(STOP_S * 1000, true, { ref: false }),
    ]);
    if (timedOut) process.kill(-this.child.pid, "SIGKILL");
  }
}

function run(command: string, args: string[], env: Record<string, string>): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: REPO_ROOT,
      env: { ...process.env, ...env },
      stdio: ["ignore", "ignore", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString()));
    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === 0) resolve();
      else reject(new ShotError(`${command} ${args.join(" ")} failed:\n${stderr.trim()}`));
    });
  });
}

async function launch(): Promise<Browser> {
  try {
    return await chromium.launch();
  } catch (e) {
    const detail = e instanceof Error ? (e.message.split("\n")[0] ?? "") : String(e);
    throw new ShotError(
      "Chromium couldn't start. Run scripts/bootstrap.sh to install it and its system " +
        `libraries (just doctor shows which is missing).\n  ${detail}`,
    );
  }
}

async function shoot(browser: Browser, url: string, outDir: string, options: ShotOptions) {
  mkdirSync(outDir, { recursive: true });
  for (const viewport of options.viewports) {
    const context = await browser.newContext(VIEWPORTS[viewport]);
    try {
      const page = await context.newPage();
      // Not "networkidle": the Activity page's event stream never goes quiet.
      await page.goto(url + options.path, { waitUntil: "load" });
      if (options.wait !== null) await page.locator(options.wait).first().waitFor();
      for (const selector of options.clicks) {
        await page.locator(selector).first().click();
        await page.waitForTimeout(SETTLE_MS);
      }
      await page.waitForTimeout(SETTLE_MS);
      const file = join(outDir, fileName(options.name, viewport));
      await page.screenshot({ path: file, fullPage: true });
      // From the repo root, as the PR draft's Screenshots section names it.
      console.log(relative(REPO_ROOT, file));
    } finally {
      await context.close();
    }
  }
}

async function main(argv: string[]): Promise<void> {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(USAGE);
    return;
  }
  const options = parseShotArgs(argv);
  let own: OwnStack | null = null;
  const stopOnSignal = () => {
    void (own?.stop() ?? Promise.resolve()).finally(() => process.exit(130));
  };
  process.once("SIGINT", stopOnSignal);
  process.once("SIGTERM", stopOnSignal);

  // First, so a missing Chromium fails before a stack is started for nothing.
  const browser = await launch();
  try {
    let url: string;
    let outDir: string;
    let env: Record<string, string> | null = null;
    if (options.base !== null) {
      // The developer's own server may show their real photos, so these stay local.
      if (!(await answers(options.base))) {
        throw new ShotError(`Nothing answers at ${options.base}. Is \`just dev\` running?`);
      }
      url = options.base;
      outDir = join(SHOTS_DIR, "local");
    } else {
      const preview = existsSync(PREVIEW_STATE) ? readState(PREVIEW_STATE) : null;
      let state: StackState;
      if (preview !== null && (await answers(preview.url))) {
        console.error(`Using the running preview at ${preview.url}`);
        state = preview;
      } else {
        console.error("Starting a scratch stack (stop it sooner with Ctrl-C)…");
        mkdirSync(SHOTS_DIR, { recursive: true });
        own = OwnStack.start();
        state = await own.ready();
      }
      url = state.url;
      env = state.env;
      outDir = join(SHOTS_DIR, "scratch");
    }

    if (options.noop > 0 && env !== null) {
      const backend = ["run", "--no-sync", "--project", "backend", "python", "-m"];
      await run("uv", [...backend, "photo_triage.worker", "noop", String(options.noop)], env);
    }

    await shoot(browser, url, outDir, options);
  } finally {
    await browser.close();
    await own?.stop();
  }
}

main(process.argv.slice(2)).catch((e: unknown) => {
  if (e instanceof UsageError) {
    console.error(`${e.message}\n\n${USAGE}`);
    process.exit(2);
  }
  console.error(e instanceof ShotError ? e.message : e);
  process.exit(1);
});
