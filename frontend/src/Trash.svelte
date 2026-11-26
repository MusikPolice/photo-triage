<script lang="ts">
  /* What's in the trash (plan §6.8), newest first: each item's thumbnail, where it
   * was, when it was trashed and when it'll be purged. Restore puts one item, or a
   * selection, back where it was. */
  import { fileName } from "./lib/item";
  import {
    allSelected,
    formatDay,
    getTrash,
    movedSummary,
    purgeWording,
    restoreItems,
    toggled,
    toggledAll,
    within,
    type TrashedItem,
  } from "./lib/trash";

  let items = $state.raw<TrashedItem[]>([]);
  let total = $state(0);
  let loaded = $state(false);
  let loadingMore = $state(false);
  let failure = $state<string | null>(null);
  let selection = $state.raw<ReadonlySet<number>>(new Set());
  /** The items being restored, so their buttons wait. */
  let restoring = $state.raw<ReadonlySet<number>>(new Set());
  let message = $state<string | null>(null);
  /** Items whose thumbnail didn't load: one never made, for a damaged file. */
  let noThumbnail = $state.raw<ReadonlySet<number>>(new Set());

  /** For "purged in 12 days"; a minute out is close enough. */
  let now = $state(new Date());
  $effect(() => {
    const timer = setInterval(() => (now = new Date()), 60_000);
    return () => {
      clearInterval(timer);
    };
  });

  const ids = $derived(items.map((item) => item.id));
  const everySelected = $derived(allSelected(selection, ids));

  async function load(more: boolean): Promise<void> {
    loadingMore = more;
    failure = null;
    try {
      const page = await getTrash(more ? items.length : 0);
      const known = new Set(more ? ids : []);
      items = [...(more ? items : []), ...page.items.filter((item) => !known.has(item.id))];
      total = page.total;
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    } finally {
      loaded = true;
      loadingMore = false;
    }
  }

  $effect(() => {
    void load(false);
  });

  async function restore(itemIds: number[]): Promise<void> {
    if (itemIds.length === 0) return;
    restoring = new Set([...restoring, ...itemIds]);
    message = null;
    try {
      const result = await restoreItems(itemIds);
      const gone = new Set(result.moved);
      items = items.filter((item) => !gone.has(item.id));
      total = Math.max(0, total - gone.size);
      selection = within(selection, ids);
      message = movedSummary("Restored", result);
    } catch (error) {
      message = `Couldn't restore: ${error instanceof Error ? error.message : String(error)}`;
    } finally {
      restoring = new Set([...restoring].filter((id) => !itemIds.includes(id)));
    }
  }

  function folderOf(path: string): string {
    return path.split("/").slice(0, -1).join("/");
  }
</script>

<div class="title">
  <h1>Trash</h1>
  {#if loaded && failure === null}
    <span class="muted">{total} {total === 1 ? "item" : "items"}</span>
  {/if}
</div>

{#if message !== null}
  <p class="message" role="status">{message}</p>
{/if}

{#if failure !== null}
  <p class="failure">The trash couldn't be loaded: {failure}</p>
{:else if !loaded}
  <p class="muted">Loading…</p>
{:else if items.length === 0}
  <p class="muted empty">
    Nothing in the trash. Items moved to the trash wait here, and can be restored, until they're
    purged for good.
  </p>
{:else}
  <div class="toolbar">
    <label class="select-all">
      <input
        type="checkbox"
        checked={everySelected}
        indeterminate={selection.size > 0 && !everySelected}
        onchange={() => {
          selection = toggledAll(selection, ids);
        }}
      />
      Select all
    </label>
    <button
      type="button"
      disabled={selection.size === 0 || restoring.size > 0}
      onclick={() => restore([...selection])}
    >
      Restore {selection.size > 0 ? `${String(selection.size)} selected` : "selected"}
    </button>
  </div>

  <ul class="items">
    {#each items as item (item.id)}
      {@const name = fileName(item.original_path)}
      {@const folder = folderOf(item.original_path)}
      <li class:selected={selection.has(item.id)}>
        <label class="thumb">
          <input
            type="checkbox"
            aria-label={`Select ${name}`}
            checked={selection.has(item.id)}
            onchange={() => {
              selection = toggled(selection, item.id);
            }}
          />
          {#if noThumbnail.has(item.id)}
            <span class="none">No preview</span>
          {:else}
            <img
              src={item.thumbnail_url}
              alt=""
              loading="lazy"
              onerror={() => {
                noThumbnail = new Set([...noThumbnail, item.id]);
              }}
            />
          {/if}
          {#if item.media_type === "video"}
            <span class="kind">Video</span>
          {/if}
        </label>
        <div class="info">
          <p class="path name" title={item.original_path}>{name}</p>
          {#if folder !== ""}
            <p class="path folder">{folder}</p>
          {/if}
          <p class="when">
            Trashed {formatDay(item.trashed_at)} ·
            <span title={formatDay(item.purge_after)}>{purgeWording(item.purge_after, now)}</span>
          </p>
          <button
            type="button"
            aria-label={`Restore ${name}`}
            disabled={restoring.has(item.id)}
            onclick={() => restore([item.id])}>Restore</button
          >
        </div>
      </li>
    {/each}
  </ul>

  {#if items.length < total}
    <button type="button" class="more" disabled={loadingMore} onclick={() => load(true)}>
      {loadingMore ? "Loading…" : `Show more (${String(total - items.length)} left)`}
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

  .empty {
    max-width: 32rem;
  }

  .message {
    margin: 0 0 1rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: 0.375rem;
    background: var(--header-bg);
    font-size: 0.875rem;
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1rem;
    margin-bottom: 0.75rem;
  }

  .select-all {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.875rem;
    cursor: pointer;
  }

  .items {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr));
    gap: 0.75rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 0.5rem;
  }

  li.selected {
    outline: 2px solid var(--map-dot);
    outline-offset: -1px;
  }

  .thumb {
    position: relative;
    display: block;
    aspect-ratio: 1;
    background: var(--border);
    cursor: pointer;
  }

  .thumb img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .none {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    font-size: 0.8125rem;
  }

  .thumb input {
    position: absolute;
    top: 0.5rem;
    left: 0.5rem;
    width: 1.25rem;
    height: 1.25rem;
    margin: 0;
    cursor: pointer;
  }

  .kind {
    position: absolute;
    right: 0.375rem;
    bottom: 0.375rem;
    padding: 0 0.375rem;
    border-radius: 0.25rem;
    background: rgb(0 0 0 / 60%);
    color: #ffffff;
    font-size: 0.75rem;
  }

  .info {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.125rem;
    padding: 0.5rem 0.625rem 0.625rem;
    font-size: 0.8125rem;
  }

  .info p {
    margin: 0;
  }

  .path {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .name {
    font-weight: 600;
  }

  .folder,
  .when {
    color: var(--muted);
  }

  .info .when {
    margin-bottom: 0.5rem;
  }

  .info button {
    align-self: flex-start;
    margin-top: auto;
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
