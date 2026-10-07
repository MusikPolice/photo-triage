import { describe, expect, it } from "vitest";

import type { StageActivity, WorkerActivity } from "../src/lib/api";
import {
  doneOfTotal,
  formatPercent,
  formatTime,
  fraction,
  stageCounts,
  stageName,
  workerLabel,
} from "../src/lib/format";

const TORONTO = { locale: "en-CA", timeZone: "America/Toronto" };
const NOW = new Date("2026-10-07T18:00:00Z"); // 14:00 Wednesday in Toronto

function stage(fields: Partial<StageActivity> = {}): StageActivity {
  return {
    stage: "thumbnail",
    kind: "item",
    paused: false,
    done: 0,
    total: 0,
    pending: 0,
    running: 0,
    errored: 0,
    parked: 0,
    ...fields,
  };
}

function worker(fields: Partial<WorkerActivity> = {}): WorkerActivity {
  return { status: "running", paused: false, quiet_until_at: null, last_seen_at: null, ...fields };
}

describe("progress", () => {
  it.each([
    [0, 0, "–"],
    [0, 40, "0.0%"],
    [1, 3, "33.3%"],
    [2, 3, "66.6%"],
    [9999, 10000, "99.9%"],
    [10000, 10000, "100%"],
  ])("%i of %i is %s", (done, total, percent) => {
    expect(formatPercent(done, total)).toBe(percent);
  });

  it("gives the bar's fraction, 0 when there's nothing to do", () => {
    expect(fraction(0, 0)).toBe(0);
    expect(fraction(1, 4)).toBe(0.25);
    expect(fraction(4, 4)).toBe(1);
  });

  it("shows done of total with separators", () => {
    expect(doneOfTotal(stage({ done: 1234, total: 250000 }), TORONTO)).toBe("1,234 / 250,000");
  });

  it("lists the counts that aren't zero", () => {
    expect(stageCounts(stage())).toBe("");
    expect(stageCounts(stage({ pending: 1200, running: 1, parked: 2 }), TORONTO)).toBe(
      "1,200 pending · 1 running · 2 parked",
    );
    expect(stageCounts(stage({ errored: 3 }))).toBe("3 errored");
  });

  it("names stages", () => {
    expect(stageName("metadata_write")).toBe("Metadata writes");
    expect(stageName("llm_tag")).toBe("LLM tagging");
  });
});

describe("formatTime", () => {
  it("shows the time alone on the same local day", () => {
    expect(formatTime(new Date("2026-10-08T03:00:00Z"), NOW, TORONTO)).toBe("23:00");
  });

  it("adds the weekday on another local day, though it's the same UTC day", () => {
    // 02:00 UTC on the 8th is Wednesday evening in Toronto; 06:00 is Thursday.
    expect(formatTime(new Date("2026-10-08T06:00:00Z"), NOW, TORONTO)).toBe("Thu 02:00");
  });
});

describe("workerLabel", () => {
  it.each([
    ["running", "Running", "ok"],
    ["paused", "Paused", "idle"],
    ["stopped", "Stopped", "alert"],
  ] as const)("says %s", (status, text, tone) => {
    expect(workerLabel(worker({ status }), "live", NOW, TORONTO)).toEqual({ text, tone });
  });

  it("says when quiet hours end, in local time", () => {
    const quiet = worker({ status: "quiet", quiet_until_at: "2026-10-08T03:00:00Z" });
    expect(workerLabel(quiet, "live", NOW, TORONTO)).toEqual({
      text: "Quiet until 23:00",
      tone: "idle",
    });
  });

  it("says when it can't reach the API, or hasn't heard yet", () => {
    expect(workerLabel(worker(), "lost", NOW).text).toBe("Offline");
    expect(workerLabel(null, "connecting", NOW).text).toBe("Connecting…");
  });
});
