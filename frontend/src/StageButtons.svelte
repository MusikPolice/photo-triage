<script lang="ts">
  import type { StageActivity } from "./lib/api";
  import { walkQueued } from "./lib/format";

  let {
    stage,
    disabled,
    onToggle,
    onScan,
  }: {
    stage: StageActivity;
    /** While another pause, resume or scan is in flight. */
    disabled: boolean;
    onToggle: () => void;
    onScan: () => void;
  } = $props();

  const queued = $derived(walkQueued(stage));
</script>

<!-- A stage card's buttons: pause or resume, and "Scan now" on the walk. -->
<div class="buttons">
  {#if stage.stage === "walk"}
    <button
      type="button"
      disabled={disabled || queued}
      title={queued ? "A walk is already queued" : undefined}
      onclick={onScan}
    >
      Scan now
    </button>
  {/if}
  <button type="button" {disabled} onclick={onToggle}>
    {stage.paused ? "Resume" : "Pause"}
  </button>
</div>

<style>
  .buttons {
    display: flex;
    flex-shrink: 0;
    gap: 0.375rem;
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
