<script lang="ts">
  import type { ActivityFeed } from "./lib/activity.svelte";
  import type { Stage } from "./lib/api";
  import {
    doneOfTotal,
    formatPercent,
    fraction,
    stageCounts,
    stageName,
    workerLabel,
  } from "./lib/format";

  let { feed }: { feed: ActivityFeed } = $props();

  const activity = $derived(feed.activity);
  const label = $derived(workerLabel(activity?.worker ?? null, feed.connection, new Date()));

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

<h1>Activity</h1>

{#if activity === null}
  <p class="muted">
    {feed.connection === "lost" ? "Can't reach the API. Retrying…" : "Loading…"}
  </p>
{:else}
  <section class="worker">
    <p>Worker: <strong>{label.text}</strong></p>
    <button
      type="button"
      disabled={busy !== null}
      onclick={() => toggle(!activity.worker.paused, null)}
    >
      {activity.worker.paused ? "Resume all" : "Pause all"}
    </button>
  </section>

  {#if failure !== null}
    <p class="failure" role="alert">{failure}</p>
  {/if}

  <ul class="stages">
    {#each activity.stages as stage (stage.stage)}
      {@const counts = stageCounts(stage)}
      <li class:paused={stage.paused}>
        <div class="head">
          <span class="name">{stageName(stage.stage)}</span>
          {#if stage.kind === "batch"}<span class="tag">batch</span>{/if}
          {#if stage.paused}<span class="tag">paused</span>{/if}
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
          <span class="muted">{counts === "" ? "Nothing queued" : counts}</span>
        </div>
      </li>
    {/each}
  </ul>
{/if}

<style>
  h1 {
    margin-top: 0;
    font-size: 1.5rem;
  }

  .muted {
    color: var(--muted);
  }

  .worker {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1rem;
    margin-bottom: 1rem;
  }

  .worker p {
    margin: 0;
  }

  .failure {
    color: var(--alert);
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

  .head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
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
    margin-left: auto;
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
