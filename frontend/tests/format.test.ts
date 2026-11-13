import { describe, expect, it } from "vitest";

import type { StageActivity, WorkerActivity } from "../src/lib/api";
import {
  barTone,
  doneOfTotal,
  formatPercent,
  formatTime,
  formatWhen,
  groupStages,
  fraction,
  stageCounts,
  stageName,
  stageRuns,
  walkQueued,
  workerHold,
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
    expect(stageName("metadata_read")).toBe("Metadata reads");
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

describe("formatWhen", () => {
  it("says today for the same local day", () => {
    expect(formatWhen(new Date("2026-10-07T06:04:00Z"), NOW, TORONTO)).toBe("today 02:04");
  });

  it("gives the weekday within six days, before or after", () => {
    expect(formatWhen(new Date("2026-10-08T06:00:00Z"), NOW, TORONTO)).toBe("Thu 02:00");
    expect(formatWhen(new Date("2026-10-03T06:00:00Z"), NOW, TORONTO)).toBe("Sat 02:00");
  });

  it("gives the date further away, so a weekday isn't ambiguous", () => {
    expect(formatWhen(new Date("2026-09-20T06:00:00Z"), NOW, TORONTO)).toBe("Sep 20 02:00");
  });
});

describe("stageRuns", () => {
  const walk = (fields: Partial<StageActivity> = {}): StageActivity =>
    stage({ stage: "walk", kind: "batch", ...fields });

  it("gives the last run and the next", () => {
    const runs = stageRuns(
      walk({
        last_finished_at: "2026-10-07T06:04:00Z",
        next_scheduled_at: "2026-10-08T06:00:00Z",
      }),
      NOW,
      TORONTO,
    );
    expect(runs).toBe("Last run today 02:04 · Next Thu 02:00");
  });

  it("says who asked for a run by hand", () => {
    const runs = stageRuns(
      walk({ last_finished_at: "2026-10-07T15:00:00Z", last_actor: "anonymous" }),
      NOW,
      TORONTO,
    );
    expect(runs).toBe("Last run today 11:00, by anonymous");
  });

  it("says a scheduled stage has never run", () => {
    expect(stageRuns(walk({ next_scheduled_at: "2026-10-08T06:00:00Z" }), NOW, TORONTO)).toBe(
      "Never run · Next Thu 02:00",
    );
  });

  it("is null for stages with no runs to show", () => {
    expect(stageRuns(stage(), NOW, TORONTO)).toBeNull();
  });
});

describe("walkQueued", () => {
  it.each([
    [{}, false],
    [{ pending: 1 }, true],
    [{ running: 1 }, true],
    [{ errored: 1 }, true],
    [{ done: 1, parked: 1 }, false],
  ] as const)("%o is %s", (fields, queued) => {
    expect(walkQueued(stage({ stage: "walk", ...fields }))).toBe(queued);
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

describe("workerHold", () => {
  it("is null while the worker runs", () => {
    expect(workerHold(worker(), NOW, TORONTO)).toBeNull();
  });

  it.each([
    ["paused", "Everything is paused.", "idle"],
    ["stopped", "The worker isn't running, so nothing is being processed.", "alert"],
  ] as const)("says why nothing runs while %s", (status, notice, tone) => {
    expect(workerHold(worker({ status }), NOW, TORONTO)).toEqual({ notice, tone });
  });

  it("says when quiet hours end, in local time", () => {
    const quiet = worker({ status: "quiet", quiet_until_at: "2026-10-08T03:00:00Z" });
    expect(workerHold(quiet, NOW, TORONTO)).toEqual({
      notice: "Quiet hours: nothing runs until 23:00.",
      tone: "idle",
    });
  });

  it("names the day when quiet hours end on another one", () => {
    const quiet = worker({ status: "quiet", quiet_until_at: "2026-10-08T06:00:00Z" });
    expect(workerHold(quiet, NOW, TORONTO)?.notice).toBe(
      "Quiet hours: nothing runs until Thu 02:00.",
    );
  });
});

describe("barTone", () => {
  const paused = { notice: "Everything is paused.", tone: "idle" } as const;
  const stopped = { notice: "The worker isn't running…", tone: "alert" } as const;

  it("is green while the worker runs, and amber for a stage's own pause", () => {
    expect(barTone(stage(), null)).toBe("ok");
    expect(barTone(stage({ paused: true }), null)).toBe("idle");
  });

  it("takes the worker's hold for every stage", () => {
    expect(barTone(stage(), paused)).toBe("idle");
    expect(barTone(stage(), stopped)).toBe("alert");
    expect(barTone(stage({ paused: true }), stopped)).toBe("alert");
  });
});

describe("groupStages", () => {
  const names = (stages: StageActivity[]) => stages.map((s) => s.stage);

  it("puts stages with work first, running ones at the top, paused ones last", () => {
    const groups = groupStages([
      stage({ stage: "metadata_write", done: 5, total: 5 }),
      stage({ stage: "scan", pending: 3, total: 3 }),
      stage({ stage: "thumbnail", paused: true, pending: 2, total: 2 }),
      stage({ stage: "clip", running: 1, pending: 4, total: 5 }),
      stage({ stage: "quality", errored: 1, total: 1 }),
      stage({ stage: "faces", parked: 2, total: 2 }),
      stage({ stage: "noop" }),
    ]);
    expect(names(groups.working)).toEqual(["clip", "scan", "quality", "thumbnail"]);
    expect(names(groups.idle)).toEqual(["metadata_write", "faces", "noop"]);
  });

  it("calls everything idle when nothing is queued", () => {
    expect(groupStages([stage()]).working).toEqual([]);
  });
});
