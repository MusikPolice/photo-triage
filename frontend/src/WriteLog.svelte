<script lang="ts">
  /* The write log (plan §6.10): every metadata write, newest first, with its file,
   * the values before and after, its status, who asked and when. A banner says
   * when writes are only logged (`EXIF_WRITES_ENABLED` off). */
  import {
    fieldChanges,
    getWriteLog,
    statusLabel,
    WRITE_STATUSES,
    type LoggedWrite,
    type WriteStatus,
  } from "./lib/writes";

  let status = $state<WriteStatus | null>(null);
  let writes = $state.raw<LoggedWrite[]>([]);
  let total = $state(0);
  let enabled = $state<boolean | null>(null);
  let loaded = $state(false);
  let loadingMore = $state(false);
  let failure = $state<string | null>(null);
  /** Counts loads, so a slow answer for an earlier filter is ignored. */
  let seq = 0;

  async function load(more: boolean, filter: WriteStatus | null = status): Promise<void> {
    const mine = ++seq;
    loadingMore = more;
    failure = null;
    try {
      const page = await getWriteLog(filter, more ? writes.length : 0);
      if (mine !== seq) return;
      const known = new Set(more ? writes.map((w) => w.id) : []);
      writes = [...(more ? writes : []), ...page.items.filter((w) => !known.has(w.id))];
      total = page.total;
      enabled = page.writes_enabled;
    } catch (error) {
      if (mine === seq) failure = error instanceof Error ? error.message : String(error);
    } finally {
      if (mine === seq) {
        loaded = true;
        loadingMore = false;
      }
    }
  }

  // Again whenever the filter changes.
  $effect(() => {
    const filter = status;
    loaded = false;
    void load(false, filter);
  });

  const when = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
</script>

<div class="title">
  <h1>Write log</h1>
  {#if loaded && failure === null}
    <span class="muted">{total} {total === 1 ? "write" : "writes"}</span>
  {/if}
</div>

{#if enabled === false}
  <p class="banner" role="note">
    <strong>Dry run.</strong> <code>EXIF_WRITES_ENABLED</code> is off, so writes are logged here but not
    made: no file has been changed.
  </p>
{/if}

<div class="filter" role="group" aria-label="Status">
  <button type="button" aria-pressed={status === null} onclick={() => (status = null)}>All</button>
  {#each WRITE_STATUSES as option (option)}
    <button type="button" aria-pressed={status === option} onclick={() => (status = option)}
      >{statusLabel(option)}</button
    >
  {/each}
</div>

{#if failure !== null}
  <p class="failure">The write log couldn't be loaded: {failure}</p>
{:else if !loaded}
  <p class="muted">Loading…</p>
{:else if writes.length === 0}
  <p class="muted">
    {status === null ? "No writes yet." : `No writes with status “${statusLabel(status)}”.`}
  </p>
{:else}
  <ul class="writes">
    {#each writes as write (write.id)}
      <li>
        <div class="head">
          <span class="path">{write.path}</span>
          <span class="status status-{write.status}">{statusLabel(write.status)}</span>
        </div>
        <dl>
          {#each fieldChanges(write) as change (change.label)}
            <dt>{change.label}</dt>
            <dd>
              {#if change.before === null}
                <span class="value">{change.after}</span>
              {:else}
                <span class="value before" title="Before">{change.before}</span>
                <span class="arrow" aria-label="becomes">→</span>
                <span class="value" title="After">{change.after}</span>
              {/if}
            </dd>
          {/each}
        </dl>
        <p class="meta muted">
          {write.actor ?? "The pipeline"} · {when.format(new Date(write.created_at))}
          {#if write.applied_at !== null && write.status !== "queued"}
            · {write.status === "dry_run" ? "logged" : "written"}
            {when.format(new Date(write.applied_at))}
          {/if}
        </p>
      </li>
    {/each}
  </ul>
  {#if writes.length < total}
    <button type="button" class="more" disabled={loadingMore} onclick={() => load(true)}>
      {loadingMore ? "Loading…" : `Show more (${String(total - writes.length)} left)`}
    </button>
  {/if}
{/if}

<style>
  .title {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
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

  .banner {
    margin: 0 0 1rem;
    padding: 0.625rem 0.875rem;
    border: 1px solid var(--idle);
    border-left-width: 4px;
    border-radius: 0.375rem;
    background: var(--header-bg);
    font-size: 0.875rem;
  }

  code {
    font-size: 0.8125rem;
  }

  .filter {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin-bottom: 1rem;
  }

  .filter button {
    border-radius: 999px;
  }

  .filter button[aria-pressed="true"] {
    color: var(--bg);
    background: var(--fg);
    border-color: var(--fg);
  }

  .writes {
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
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .path {
    min-width: 0;
    overflow-wrap: anywhere;
    font-family: ui-monospace, monospace;
    font-size: 0.8125rem;
  }

  .status {
    flex-shrink: 0;
    padding: 0 0.5rem;
    border: 1px solid var(--tone, var(--border));
    border-radius: 999px;
    color: var(--tone, var(--muted));
    font-size: 0.75rem;
    white-space: nowrap;
  }

  .status-applied {
    --tone: var(--ok);
  }

  .status-dry_run {
    --tone: var(--idle);
  }

  .status-error {
    --tone: var(--alert);
  }

  dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 0.25rem 0.75rem;
    margin: 0.5rem 0 0;
    font-size: 0.875rem;
  }

  dt {
    color: var(--muted);
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .before {
    color: var(--muted);
  }

  .arrow {
    margin: 0 0.25rem;
    color: var(--muted);
  }

  .meta {
    margin: 0.5rem 0 0;
    font-size: 0.8125rem;
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
    cursor: default;
    opacity: 0.6;
  }

  .more {
    display: block;
    margin: 1rem auto 0;
  }
</style>
