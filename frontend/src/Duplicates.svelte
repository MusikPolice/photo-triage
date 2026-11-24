<script lang="ts">
  /* Duplicate review (plan §6.7): one pending group at a time, held for this tab
   * while it's open. Side by side on a desktop; on a phone, one member at a time,
   * swiped through. The recommended keeper is marked and selected to start with. */
  import {
    decideGroup,
    defaultKeep,
    getNextGroup,
    memberFacts,
    releaseGroup,
    reviewedSummary,
    reviewerToken,
    trashCount,
    type Action,
    type NextGroup,
  } from "./lib/duplicates";
  import { fileName, formatTakenAt } from "./lib/item";

  const reviewer = reviewerToken(safeSessionStorage());

  function safeSessionStorage(): Storage | null {
    try {
      return sessionStorage;
    } catch {
      return null;
    }
  }

  let next = $state.raw<NextGroup | null>(null);
  let failure = $state<string | null>(null);
  let busy = $state(false);
  let message = $state<string | null>(null);
  let keep = $state.raw<ReadonlySet<number>>(new Set());
  /** The member in view on a phone, from the strip's scroll. */
  let shown = $state(0);
  let strip = $state<HTMLUListElement | null>(null);

  const group = $derived(next?.group ?? null);
  const members = $derived(group?.members ?? []);
  const toTrash = $derived(trashCount(keep, members));

  async function load(): Promise<void> {
    failure = null;
    try {
      next = await getNextGroup(reviewer);
      keep = defaultKeep(next.group?.members ?? []);
      shown = 0;
      strip?.scrollTo({ left: 0 });
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    }
  }

  $effect(() => {
    void load();
  });

  // Let the group go when the page is left, in the app or by closing the tab.
  $effect(() => {
    const id = group?.id;
    if (id === undefined) return;
    const release = () => {
      releaseGroup(id, reviewer);
    };
    window.addEventListener("pagehide", release);
    return () => {
      window.removeEventListener("pagehide", release);
      // Not after a decision: that group is no longer held.
      if (id !== decided) release();
    };
  });
  /** The group last decided here, which needs no release. Not state: the effect
   * reads it only as it ends. */
  let decided: number | null = null;

  async function decide(action: Action): Promise<void> {
    if (group === null) return;
    busy = true;
    message = null;
    try {
      const result = await decideGroup(group.id, reviewer, action, [...keep]);
      decided = group.id;
      const moved = result.moved.length;
      const left = result.skipped.length;
      message =
        action === "skip"
          ? "Skipped."
          : action === "keep_all"
            ? "Kept them all."
            : `Moved ${String(moved)} to the trash.` +
              (left > 0 ? ` ${String(left)} couldn't be: ${result.skipped[0]?.reason ?? ""}` : "");
      await load();
    } catch (error) {
      message = `Couldn't do that: ${error instanceof Error ? error.message : String(error)}`;
    } finally {
      busy = false;
    }
  }

  function onScroll(): void {
    if (strip === null) return;
    const width = strip.firstElementChild?.getBoundingClientRect().width ?? strip.clientWidth;
    shown = Math.round(strip.scrollLeft / Math.max(width, 1));
  }
</script>

<div class="title">
  <h1>Duplicates</h1>
  {#if next !== null && next.total > 0}
    <span class="muted">{reviewedSummary(next.pending, next.total)}</span>
  {/if}
</div>

{#if message !== null}
  <p class="message" role="status">{message}</p>
{/if}

{#if failure !== null}
  <p class="failure">Duplicates couldn't be loaded: {failure}</p>
{:else if next === null}
  <p class="muted">Loading…</p>
{:else if group === null}
  <p class="muted">
    {next.pending > 0
      ? "The groups left are open in another window."
      : "No duplicates to review. Groups appear here as the library is compared."}
  </p>
{:else}
  <p class="hint muted">
    {members.length} near-identical items. Tick the ones to keep; the rest go to the trash.
  </p>
  <ul class="members" bind:this={strip} onscroll={onScroll} aria-label="Members">
    {#each members as member, i (member.id)}
      {@const name = fileName(member.path)}
      <li class:keep={keep.has(member.id)} aria-label={`Member ${String(i + 1)}`}>
        <div class="picture">
          <img
            src={member.media_type === "video" ? member.thumbnail_url : member.view_url}
            alt={name}
            loading="lazy"
          />
          {#if member.rank === 0}<span class="badge">Recommended</span>{/if}
          {#if member.media_type === "video"}<span class="kind">Video</span>{/if}
        </div>
        <label class="choice">
          <input
            type="checkbox"
            checked={keep.has(member.id)}
            onchange={() => {
              const changed = new Set(keep);
              if (!changed.delete(member.id)) changed.add(member.id);
              keep = changed;
            }}
          />
          Keep
        </label>
        <p class="path">{member.path}</p>
        <p class="facts muted">
          {[
            ...memberFacts(member),
            member.taken_at_local === null ? "No date" : formatTakenAt(member.taken_at_local),
          ].join(" · ")}
        </p>
      </li>
    {/each}
  </ul>
  <p class="dots muted" aria-live="polite">{shown + 1} of {members.length}</p>

  <div class="actions">
    <button
      type="button"
      class="primary"
      disabled={busy || keep.size === 0 || toTrash === 0}
      onclick={() => decide("keep_selected")}
    >
      Keep selected, trash {toTrash}
    </button>
    <button type="button" disabled={busy} onclick={() => decide("keep_all")}>Keep all</button>
    <button type="button" disabled={busy} onclick={() => decide("skip")}>Skip</button>
  </div>
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

  .message {
    margin: 0 0 1rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: 0.375rem;
    background: var(--header-bg);
    font-size: 0.875rem;
  }

  .hint {
    margin: 0 0 0.75rem;
    font-size: 0.875rem;
  }

  .members {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: 0.75rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    padding: 0.5rem;
    border: 2px solid var(--border);
    border-radius: 0.5rem;
  }

  li.keep {
    border-color: var(--ok);
  }

  .picture {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    aspect-ratio: 4 / 3;
    overflow: hidden;
    border-radius: 0.25rem;
    background: #0b0c0d;
  }

  .picture img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }

  .badge,
  .kind {
    position: absolute;
    padding: 0.05rem 0.45rem;
    border-radius: 0.25rem;
    font-size: 0.75rem;
  }

  .badge {
    top: 0.375rem;
    left: 0.375rem;
    color: #ffffff;
    background: var(--ok);
  }

  .kind {
    right: 0.375rem;
    bottom: 0.375rem;
    color: #ffffff;
    background: rgb(0 0 0 / 60%);
  }

  .choice {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-top: 0.25rem;
    font-weight: 600;
    cursor: pointer;
  }

  .choice input {
    width: 1.125rem;
    height: 1.125rem;
    margin: 0;
  }

  li p {
    margin: 0;
    font-size: 0.8125rem;
  }

  .path {
    overflow-wrap: anywhere;
  }

  .dots {
    display: none;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 1rem;
  }

  button {
    padding: 0.45rem 0.9rem;
    border: 1px solid var(--border);
    border-radius: 0.375rem;
    background: var(--header-bg);
    color: inherit;
    font: inherit;
    font-size: 0.875rem;
    cursor: pointer;
  }

  button.primary {
    color: var(--bg);
    background: var(--fg);
    border-color: var(--fg);
  }

  button:disabled {
    cursor: default;
    opacity: 0.5;
  }

  /* Phones: one member at a time, swiped through. */
  @media (max-width: 720px) {
    .members {
      display: flex;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      overscroll-behavior-x: contain;
      gap: 0.5rem;
    }

    li {
      flex: 0 0 100%;
      box-sizing: border-box;
      scroll-snap-align: center;
    }

    .dots {
      display: block;
      margin: 0.375rem 0 0;
      font-size: 0.8125rem;
      text-align: center;
    }

    .actions button {
      flex: 1 1 auto;
    }
  }
</style>
