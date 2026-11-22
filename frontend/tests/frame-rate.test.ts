import { describe, expect, it } from "vitest";

import { FrameMeter, wantsFrameRate } from "../src/lib/frame-rate";

describe("the frame-rate readout", () => {
  it("is shown only when the URL asks", () => {
    expect(wantsFrameRate("?fps")).toBe(true);
    expect(wantsFrameRate("?item=3&fps=1")).toBe(true);
    expect(wantsFrameRate("?item=3")).toBe(false);
    expect(wantsFrameRate("")).toBe(false);
  });

  it("reads 60 fps from frames 1/60 s apart, once a second", () => {
    const meter = new FrameMeter();
    const readings = [];
    for (let n = 0; n <= 120; n++) {
      const reading = meter.frame((n * 1000) / 60);
      if (reading !== null) readings.push(reading);
    }
    expect(readings).toHaveLength(2);
    expect(readings[0]?.fps).toBe(60);
    expect(readings[0]?.worstMs).toBeCloseTo(1000 / 60);
  });

  it("reports a stutter as the worst frame", () => {
    const meter = new FrameMeter();
    const times = [0, 16, 32, 232, 248, 1000, 1016];
    const readings = times.map((t) => meter.frame(t)).filter((r) => r !== null);
    expect(readings).toEqual([{ fps: 5, worstMs: 752 }]);
  });
});
