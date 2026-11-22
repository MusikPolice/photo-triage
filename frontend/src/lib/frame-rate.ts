/** A frame-rate readout for trying the map on a phone (#119): add `?fps` to the URL.
 * Counts the browser's animation frames each second, and the slowest one, which is
 * what a stutter while panning looks like. */

/** Whether a URL's query asks for the readout. */
export function wantsFrameRate(search: string): boolean {
  return new URLSearchParams(search).has("fps");
}

export interface FrameRate {
  /** Frames in the last whole second. */
  fps: number;
  /** The longest gap between two of them, in milliseconds. */
  worstMs: number;
}

/** Feed it each frame's timestamp (`requestAnimationFrame`'s); it returns a reading
 * once a second has passed, and null otherwise. */
export class FrameMeter {
  private windowStart: number | null = null;
  private last: number | null = null;
  private frames = 0;
  private worstMs = 0;

  frame(nowMs: number): FrameRate | null {
    if (this.windowStart === null || this.last === null) {
      this.windowStart = nowMs;
      this.last = nowMs;
      return null;
    }
    this.frames += 1;
    this.worstMs = Math.max(this.worstMs, nowMs - this.last);
    this.last = nowMs;
    const elapsedMs = nowMs - this.windowStart;
    if (elapsedMs < 1000) return null;
    const reading = { fps: Math.round((this.frames * 1000) / elapsedMs), worstMs: this.worstMs };
    this.windowStart = nowMs;
    this.frames = 0;
    this.worstMs = 0;
    return reading;
  }
}
