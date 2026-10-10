<script lang="ts">
  import type { ActivityFeed } from "./lib/activity.svelte";
  import type { Stage, StageActivity } from "./lib/api";
  import {
    doneOfTotal,
    formatCount,
    formatPercent,
    fraction,
    groupStages,
    stageCounts,
    stageName,
  } from "./lib/format";

  let { feed }: { feed: ActivityFeed } = $props();

  const activity = $derived(feed.activity);
  const groups = $derived(groupStages(activity?.stages ?? []));

  /** What an idle stage has to show: its done and parked counts. */
  function idleSummary(stage: StageActivity): string {
    const parts = [];
    if (stage.done > 0) parts.push(`${formatCount(stage.done)} done`);
    if (stage.parked > 0) parts.push(`${formatCount(stage.parked)} parked`);
    return parts.length > 0 ? parts.join(" · ") : "Nothing yet";
  }

  /** The pause or resume in flight: "all", a stage, or null. */
  let busy = $state<Stage | "all" | null>(null);
  let failure = $state<string | null>(null);

  async function toggle(paused: boolean, stage: Stage | null): Promise<void> {
    busy = stage ?? "all";
    failure = null;
    try {
      await feed.setPaused(paused, stage);
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    } finally {
      busy = null;
    }
  }
</script>

<!-- The worker's status is in the header's status indicator, so this page doesn't repeat it. -->
<div class="title">
  <h1>Activity</h1>
  {#if activity !== null}
    <button
      type="button"
      disabled={busy !== null}
      onclick={() => toggle(!activity.worker.paused, null)}
    >
      {activity.worker.paused ? "Resume all" : "Pause all"}
    </button>
  {/if}
</div>

{#if activity === null}
  <p class="muted">
    {feed.connection === "lost" ? "Can't reach the API. Retrying…" : "Loading…"}
  </p>
{:else}
  {#if failure !== null}
    <p class="failure" role="alert">{failure}</p>
  {/if}

  <h2>Working</h2>
  {#if groups.working.length === 0}
    <p class="muted">Nothing queued.</p>
  {:else}
    <ul class="stages">
      {#each groups.working as stage (stage.stage)}
        {@const counts = stageCounts(stage)}
        <li class:paused={stage.paused}>
          <div class="head">
            <div class="label">
              <span class="name">{stageName(stage.stage)}</span>
              {#if stage.kind === "batch"}<span class="tag">batch</span>{/if}
              {#if stage.paused}<span class="tag">paused</span>{/if}
            </div>
            <button
              type="button"
              disabled={busy !== null}
              onclick={() => toggle(!stage.paused, stage.stage)}
            >
              {stage.paused ? "Resume" : "Pause"}
            </button>
          </div>
          <div
            class="bar"
            role="progressbar"
            aria-label={stageName(stage.stage)}
            aria-valuemin={0}
            aria-valuemax={stage.total}
            aria-valuenow={stage.done}
          >
            <div style:width="{fraction(stage.done, stage.total) * 100}%"></div>
          </div>
          <div class="numbers">
            <span>{doneOfTotal(stage)} · {formatPercent(stage.done, stage.total)}</span>
            <span class="muted">{counts}</span>
          </div>
        </li>
      {/each}
    </ul>
  {/if}

  {#if groups.idle.length > 0}
    <h2>Idle</h2>
    <ul class="stages idle">
      {#each groups.idle as stage (stage.stage)}
        <li>
          <div class="head">
            <div class="label">
              <span class="name">{stageName(stage.stage)}</span>
              {#if stage.kind === "batch"}<span class="tag">batch</span>{/if}
              {#if stage.paused}<span class="tag">paused</span>{/if}
              <span class="muted summary">{idleSummary(stage)}</span>
            </div>
            <button
              type="button"
              disabled={busy !== null}
              onclick={() => toggle(!stage.paused, stage.stage)}
            >
              {stage.paused ? "Resume" : "Pause"}
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
{/if}

<style>
  .title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1rem;
    margin-bottom: 1rem;
  }

  h1 {
    margin: 0;
    font-size: 1.5rem;
  }

  .muted {
    color: var(--muted);
  }

  .failure {
    color: var(--alert);
  }

  h2 {
    margin: 1.25rem 0 0.5rem;
    font-size: 0.875rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .stages {
    display: grid;
    gap: 0.75rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    padding: 0.75rem;
    border: 1px solid var(--border);
    border-radius: 0.5rem;
  }

  /* The button keeps the top right; the label wraps in the space to its left. */
  .head {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
  }

  .label {
    display: flex;
    flex: 1;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 0.5rem;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .idle {
    gap: 0.375rem;
  }

  .idle li {
    padding: 0.375rem 0.75rem;
    opacity: 0.6;
  }

  .idle li:hover,
  .idle li:focus-within {
    opacity: 1;
  }

  .summary {
    font-size: 0.875rem;
  }

  .name {
    font-weight: 600;
  }

  .tag {
    padding: 0 0.4rem;
    border: 1px solid var(--border);
    border-radius: 0.25rem;
    color: var(--muted);
    font-size: 0.75rem;
  }

  .head button {
    flex-shrink: 0;
  }

  .bar {
    height: 0.5rem;
    margin: 0.5rem 0;
    overflow: hidden;
    border-radius: 0.25rem;
    background: var(--border);
  }

  .bar div {
    height: 100%;
    background: var(--ok);
  }

  .paused .bar div {
    background: var(--idle);
  }

  .numbers {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0 1rem;
    font-size: 0.875rem;
  }

  button {
    padding: 0.3rem 0.8rem;
    border: 1px solid var(--border);
    border-radius: 0.375rem;
    background: var(--header-bg);
    color: inherit;
    font: inherit;
    font-size: 0.875rem;
    cursor: pointer;
  }

  button:disabled {
    cursor: wait;
    opacity: 0.6;
  }
</style>
