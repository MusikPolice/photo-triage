<script lang="ts">
  /* A slider with two ends, for a range of months (plan §6.4 "Filters"). It works by
   * pointer, so mouse and touch alike: a tap or press on the track moves the nearer
   * end there, and dragging carries it. Each end is also a slider for the keyboard:
   * arrows move a month, Page Up and Page Down a year, Home and End to the limit. */
  import {
    fractionOf,
    formatMonth,
    monthAt,
    moveEnd,
    nearerEnd,
    type Month,
    type MonthRange,
  } from "./lib/date-filter";

  interface Props {
    /** The months it spans. */
    span: MonthRange;
    /** The range chosen. */
    range: MonthRange;
    onChange: (range: MonthRange) => void;
  }

  const { span, range, onChange }: Props = $props();

  let track = $state<HTMLDivElement | null>(null);
  /** The end being dragged, while a pointer is down. */
  let dragging: "from" | "to" | null = null;

  function monthUnder(event: PointerEvent): Month {
    if (track === null) return span.from;
    const box = track.getBoundingClientRect();
    return monthAt((event.clientX - box.left) / box.width, span);
  }

  function change(next: MonthRange): void {
    if (next.from !== range.from || next.to !== range.to) onChange(next);
  }

  function onPointerDown(event: PointerEvent): void {
    if (event.button !== 0) return;
    const month = monthUnder(event);
    dragging = nearerEnd(range, month);
    track?.setPointerCapture(event.pointerId);
    change(moveEnd(range, dragging, month));
  }

  function onPointerMove(event: PointerEvent): void {
    if (dragging !== null) change(moveEnd(range, dragging, monthUnder(event)));
  }

  function onPointerUp(): void {
    dragging = null;
  }

  const KEY_STEPS: Record<string, number> = {
    ArrowLeft: -1,
    ArrowDown: -1,
    ArrowRight: 1,
    ArrowUp: 1,
    PageDown: -12,
    PageUp: 12,
  };

  function onKeyDown(end: "from" | "to", event: KeyboardEvent): void {
    const at = range[end];
    let month: Month;
    if (event.key === "Home") month = span.from;
    else if (event.key === "End") month = span.to;
    else {
      const step = KEY_STEPS[event.key];
      if (step === undefined) return;
      month = Math.min(Math.max(at + step, span.from), span.to);
    }
    event.preventDefault();
    change(moveEnd(range, end, month));
  }

  const fromPct = $derived(fractionOf(range.from, span) * 100);
  const toPct = $derived(fractionOf(range.to, span) * 100);
</script>

<div
  class="slider"
  bind:this={track}
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
  role="group"
  aria-label="Dates"
>
  <div class="rail"></div>
  <div class="chosen" style:left="{fromPct}%" style:width="{toPct - fromPct}%"></div>
  {#each [["from", fromPct, "From"], ["to", toPct, "To"]] as const as [end, pct, label] (end)}
    <div
      class="thumb"
      role="slider"
      tabindex="0"
      aria-label={label}
      aria-valuemin={span.from}
      aria-valuemax={span.to}
      aria-valuenow={range[end]}
      aria-valuetext={formatMonth(range[end])}
      style:left="{pct}%"
      onkeydown={(event) => {
        onKeyDown(end, event);
      }}
    ></div>
  {/each}
</div>

<style>
  .slider {
    position: relative;
    height: 2.25rem;
    /* Room for the thumbs at either end. */
    margin: 0 0.75rem;
    /* The slider handles horizontal drags itself; the page may still scroll. */
    touch-action: pan-y;
    cursor: pointer;
  }

  .rail,
  .chosen {
    position: absolute;
    top: 50%;
    height: 0.25rem;
    border-radius: 999px;
    transform: translateY(-50%);
  }

  .rail {
    right: 0;
    left: 0;
    background: var(--border);
  }

  .chosen {
    background: var(--fg);
  }

  .thumb {
    position: absolute;
    top: 50%;
    width: 1.375rem;
    height: 1.375rem;
    background: var(--bg);
    border: 2px solid var(--fg);
    border-radius: 50%;
    transform: translate(-50%, -50%);
  }

  .thumb:focus-visible {
    outline: 2px solid var(--map-dot);
    outline-offset: 2px;
  }
</style>
