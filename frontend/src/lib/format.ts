/** Words and numbers for the status indicator and the Activity page (plan §7). */

import type { Stage, StageActivity, WorkerActivity } from "./api";
import type { Connection } from "./activity.svelte";

/** Where the formatting comes from. Tests fix both; the app uses the browser's. */
export interface Locale {
  locale?: string;
  timeZone?: string;
}

const STAGE_NAMES: Record<Stage, string> = {
  metadata_write: "Metadata writes",
  scan: "Scan",
  thumbnail: "Thumbnails",
  clip: "CLIP",
  quality: "Quality",
  faces: "Faces",
  recognize: "Recognition",
  layout: "Layout re-fit",
  duplicates: "Duplicate grouping",
  atlases: "Atlases",
  llm_tag: "LLM tagging",
  noop: "Noop (dev)",
};

export function stageName(stage: Stage): string {
  return STAGE_NAMES[stage];
}

export function formatCount(n: number, { locale }: Locale = {}): string {
  return new Intl.NumberFormat(locale).format(n);
}

/** Done as a share of total, rounded down so it reads 100% only when all are done. */
export function formatPercent(done: number, total: number): string {
  if (total === 0) return "–";
  if (done >= total) return "100%";
  return `${(Math.floor((done * 1000) / total) / 10).toFixed(1)}%`;
}

/** For the progress bar's width: 0 to 1, and 0 when there's nothing to do. */
export function fraction(done: number, total: number): number {
  return total === 0 ? 0 : Math.min(done / total, 1);
}

export function doneOfTotal(stage: StageActivity, locale: Locale = {}): string {
  return `${formatCount(stage.done, locale)} / ${formatCount(stage.total, locale)}`;
}

/** The non-zero counts, e.g. "3 pending · 1 running · 2 parked". */
export function stageCounts(stage: StageActivity, locale: Locale = {}): string {
  const counts: [number, string][] = [
    [stage.pending, "pending"],
    [stage.running, "running"],
    [stage.errored, "errored"],
    [stage.parked, "parked"],
  ];
  return counts
    .filter(([n]) => n > 0)
    .map(([n, label]) => `${formatCount(n, locale)} ${label}`)
    .join(" · ");
}

function dayKey(at: Date, timeZone: string | undefined): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, dateStyle: "short" }).format(at);
}

/** "23:00" if `at` is on the same local day as `now`, else "Sat 07:00". */
export function formatTime(at: Date, now: Date, { locale, timeZone }: Locale = {}): string {
  const sameDay = dayKey(at, timeZone) === dayKey(now, timeZone);
  return new Intl.DateTimeFormat(locale, {
    timeZone,
    weekday: sameDay ? undefined : "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(at);
}

export type Tone = "ok" | "idle" | "alert";

export interface WorkerLabel {
  text: string;
  /** For the indicator's colour: working, holding back on purpose, or a problem. */
  tone: Tone;
}

/** What the header's status indicator says. */
export function workerLabel(
  worker: WorkerActivity | null,
  connection: Connection,
  now: Date,
  locale: Locale = {},
): WorkerLabel {
  if (connection === "lost") return { text: "Offline", tone: "alert" };
  if (worker === null) return { text: "Connecting…", tone: "idle" };
  switch (worker.status) {
    case "running":
      return { text: "Running", tone: "ok" };
    case "paused":
      return { text: "Paused", tone: "idle" };
    case "quiet":
      return worker.quiet_until_at === null
        ? { text: "Quiet hours", tone: "idle" }
        : {
            text: `Quiet until ${formatTime(new Date(worker.quiet_until_at), now, locale)}`,
            tone: "idle",
          };
    case "stopped":
      return { text: "Stopped", tone: "alert" };
  }
}
