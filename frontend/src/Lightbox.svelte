<script lang="ts">
  /* An item at full size over the map, with what's known about it (plan §6.4
   * "Lightbox"), and a strip of the items most like it: opening one walks through
   * the library. A modal dialog: Escape, the close button or the back button
   * closes it. */
  import {
    fileName,
    formatBytes,
    formatDimensions,
    formatDuration,
    formatTakenAt,
    getItem,
    getNeighbours,
    viewUrl,
    type ItemDetails,
    type Neighbours,
  } from "./lib/item";
  import { trashItems } from "./lib/trash";

  interface Props {
    itemId: number;
    /** Opens another item in the lightbox: a similar one, from the strip. */
    onOpen: (itemId: number) => void;
    onClose: () => void;
    /** Called once the item has been moved to the trash. */
    onTrashed: (itemId: number) => void;
  }

  const { itemId, onOpen, onClose, onTrashed }: Props = $props();

  let trashing = $state(false);
  let trashFailure = $state<string | null>(null);

  async function moveToTrash(id: number): Promise<void> {
    trashing = true;
    trashFailure = null;
    try {
      const result = await trashItems([id]);
      if (result.moved.includes(id)) onTrashed(id);
      else trashFailure = result.skipped[0]?.reason ?? "It wasn't moved";
    } catch (error) {
      trashFailure = error instanceof Error ? error.message : String(error);
    } finally {
      trashing = false;
    }
  }

  let dialog = $state<HTMLDialogElement | null>(null);
  let item = $state.raw<ItemDetails | null>(null);
  let failure = $state<string | null>(null);
  let mediaFailed = $state(false);
  let neighbours = $state.raw<Neighbours | null>(null);
  let neighboursFailed = $state(false);

  $effect(() => {
    const id = itemId;
    item = null;
    failure = null;
    mediaFailed = false;
    neighbours = null;
    neighboursFailed = false;
    trashFailure = null;
    let current = true;
    getNeighbours(id).then(
      (loaded) => {
        if (current) neighbours = loaded;
      },
      () => {
        if (current) neighboursFailed = true;
      },
    );
    getItem(id).then(
      (loaded) => {
        if (current) item = loaded;
      },
      (error: unknown) => {
        if (current) failure = error instanceof Error ? error.message : String(error);
      },
    );
    return () => {
      current = false;
    };
  });

  $effect(() => {
    if (dialog === null || dialog.open) return;
    dialog.showModal();
    // The dialog itself, not its close button, so the button shows no focus ring
    // until the keyboard reaches it. Tab still goes to it first.
    dialog.focus();
  });

  const folder = $derived(item === null ? "" : item.path.split("/").slice(0, -1).join("/"));
  const dimensions = $derived(item === null ? null : formatDimensions(item.width, item.height));
</script>

<dialog
  bind:this={dialog}
  class="lightbox"
  tabindex="-1"
  aria-label={item === null ? "Item" : fileName(item.path)}
  oncancel={(event) => {
    // Escape: closed by the page, through the history, not by the browser.
    event.preventDefault();
    onClose();
  }}
>
  <div class="media">
    {#if failure !== null}
      <p class="message">This item couldn't be loaded: {failure}</p>
    {:else if item !== null && mediaFailed}
      <p class="message">This {item.media_type} can't be shown.</p>
    {:else if item?.media_type === "video"}
      <!-- Home videos come with no captions to show. -->
      <!-- svelte-ignore a11y_media_has_caption -->
      <video
        src={viewUrl(item.id)}
        controls
        playsinline
        preload="metadata"
        onerror={() => {
          mediaFailed = true;
        }}
      ></video>
    {:else if item !== null}
      <img
        src={viewUrl(item.id)}
        alt={fileName(item.path)}
        onerror={() => {
          mediaFailed = true;
        }}
      />
    {/if}
    <button
      class="close"
      type="button"
      aria-label="Close"
      onclick={() => {
        onClose();
      }}>×</button
    >
  </div>

  <section class="details" aria-label="Details">
    {#if item !== null}
      <div class="heading">
        <h2>
          {item.taken_at_local === null ? "Unknown date" : formatTakenAt(item.taken_at_local)}
        </h2>
        <button type="button" class="trash" disabled={trashing} onclick={() => moveToTrash(itemId)}>
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path
              d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          {trashing ? "Moving…" : "Move to trash"}
        </button>
      </div>
      {#if trashFailure !== null}
        <p class="failure" role="alert">Couldn't move it to the trash: {trashFailure}</p>
      {/if}
      <section class="similar" aria-label="Similar">
        <h3>Similar</h3>
        {#if neighboursFailed}
          <p class="note">Similar items couldn't be loaded.</p>
        {:else if neighbours === null}
          <p class="note">Loading…</p>
        {:else if !neighbours.embedded}
          <p class="note">Not compared with the others yet.</p>
        {:else if neighbours.items.length === 0}
          <p class="note">Nothing else to compare it with yet.</p>
        {:else}
          <ul>
            {#each neighbours.items as neighbour, i (neighbour.id)}
              <li>
                <button
                  type="button"
                  aria-label={`Similar ${neighbour.media_type} ${String(i + 1)}`}
                  onclick={() => {
                    onOpen(neighbour.id);
                  }}
                >
                  <img src={neighbour.thumbnail_url} alt="" loading="lazy" />
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
      <dl>
        <dt>File</dt>
        <dd class="path">{fileName(item.path)}</dd>
        {#if folder !== ""}
          <dt>Folder</dt>
          <dd class="path">{folder}</dd>
        {/if}
        {#if dimensions !== null}
          <dt>Size</dt>
          <dd>{dimensions}</dd>
        {/if}
        {#if item.duration_s !== null}
          <dt>Length</dt>
          <dd>{formatDuration(item.duration_s)}</dd>
        {/if}
        <dt>File size</dt>
        <dd>{formatBytes(item.file_size_bytes)}</dd>
      </dl>
      {#if item.damaged}
        <p class="note">The file is cut short: only the top of the picture is there.</p>
      {/if}
      {#if item.tags.length > 0}
        <section aria-label="Tags">
          <h3>Tags</h3>
          <ul class="tags">
            {#each item.tags as tag (tag)}
              <li>{tag}</li>
            {/each}
          </ul>
        </section>
      {/if}
    {:else if failure === null}
      <p class="note">Loading…</p>
    {/if}
  </section>
</dialog>

<style>
  .lightbox {
    /* The whole screen: the picture gets all it can. */
    box-sizing: border-box;
    width: 100vw;
    max-width: none;
    height: 100dvh;
    max-height: none;
    margin: 0;
    padding: 0;
    border: 0;
    color: var(--fg);
    background: var(--bg);
  }

  .lightbox[open] {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 20rem;
  }

  .lightbox::backdrop {
    background: rgb(0 0 0 / 60%);
  }

  .media {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 0;
    /* Pictures look truest on near-black, whatever the theme. */
    background: #0b0c0d;
  }

  .media img,
  .media video {
    display: block;
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }

  .message {
    padding: 0 1.5rem;
    color: #c9ccd1;
    text-align: center;
  }

  .close {
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    width: 2.75rem;
    height: 2.75rem;
    padding: 0;
    font-size: 1.75rem;
    line-height: 1;
    color: #ffffff;
    background: rgb(0 0 0 / 55%);
    border: 1px solid rgb(255 255 255 / 35%);
    border-radius: 50%;
    cursor: pointer;
  }

  .close:hover {
    background: rgb(0 0 0 / 75%);
  }

  .close:focus-visible {
    outline: 2px solid #ffffff;
    outline-offset: 2px;
  }

  .details {
    overflow-y: auto;
    padding: 1.25rem 1.25rem 2rem;
    border-left: 1px solid var(--border);
  }

  .heading {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem 1rem;
    margin: 0 0 1rem;
  }

  h2 {
    margin: 0;
    font-size: 1.125rem;
    font-weight: 600;
  }

  .trash {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.3rem 0.7rem;
    color: inherit;
    font: inherit;
    font-size: 0.875rem;
    white-space: nowrap;
    background: var(--header-bg);
    border: 1px solid var(--border);
    border-radius: 0.375rem;
    cursor: pointer;
  }

  .trash:hover {
    border-color: var(--alert);
    color: var(--alert);
  }

  .trash:disabled {
    cursor: default;
    opacity: 0.6;
  }

  .failure {
    margin: -0.5rem 0 1rem;
    font-size: 0.875rem;
    color: var(--alert);
  }

  h3 {
    margin: 1.25rem 0 0.5rem;
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--muted);
  }

  dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 0.375rem 1rem;
    margin: 0;
    font-size: 0.875rem;
  }

  dt {
    color: var(--muted);
  }

  dd {
    margin: 0;
  }

  .path {
    overflow-wrap: anywhere;
  }

  .note {
    margin: 1rem 0 0;
    font-size: 0.875rem;
    color: var(--muted);
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .similar {
    margin-bottom: 1.25rem;
  }

  .similar h3 {
    margin-top: 0;
  }

  .similar .note {
    margin: 0;
  }

  .similar ul {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.375rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .similar button {
    display: block;
    width: 100%;
    aspect-ratio: 1;
    padding: 0;
    overflow: hidden;
    background: var(--border);
    border: 0;
    border-radius: 0.375rem;
    cursor: pointer;
  }

  .similar button:focus-visible {
    outline: 2px solid var(--fg);
    outline-offset: 2px;
  }

  .similar img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .tags li {
    padding: 0.125rem 0.625rem;
    font-size: 0.8125rem;
    border: 1px solid var(--border);
    border-radius: 999px;
  }

  /* Phones: the picture on top, the details below it. */
  @media (max-width: 720px) {
    .lightbox[open] {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr) auto;
    }

    /* One row, scrolled sideways, so the strip doesn't push the details away. */
    .similar ul {
      grid-template-columns: none;
      grid-auto-columns: 4.5rem;
      grid-auto-flow: column;
      overflow-x: auto;
    }

    .details {
      max-height: 40dvh;
      border-top: 1px solid var(--border);
      border-left: 0;
    }
  }
</style>
