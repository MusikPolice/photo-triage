<script lang="ts">
  /* An item at full size over the map, with what's known about it (plan §6.4
   * "Lightbox"). A modal dialog: Escape, the close button or the back button
   * closes it. */
  import {
    fileName,
    formatBytes,
    formatDimensions,
    formatDuration,
    formatTakenAt,
    getItem,
    viewUrl,
    type ItemDetails,
  } from "./lib/item";

  interface Props {
    itemId: number;
    onClose: () => void;
  }

  const { itemId, onClose }: Props = $props();

  let dialog = $state<HTMLDialogElement | null>(null);
  let item = $state.raw<ItemDetails | null>(null);
  let failure = $state<string | null>(null);
  let mediaFailed = $state(false);

  $effect(() => {
    const id = itemId;
    item = null;
    failure = null;
    mediaFailed = false;
    let current = true;
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
      <h2>
        {item.taken_at_local === null ? "Unknown date" : formatTakenAt(item.taken_at_local)}
      </h2>
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
      {#if item.keywords.length > 0}
        <h3>Keywords</h3>
        <ul class="keywords">
          {#each item.keywords as keyword (keyword)}
            <li>{keyword}</li>
          {/each}
        </ul>
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

  h2 {
    margin: 0 0 1rem;
    font-size: 1.125rem;
    font-weight: 600;
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

  .keywords {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .keywords li {
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

    .details {
      max-height: 40dvh;
      border-top: 1px solid var(--border);
      border-left: 0;
    }
  }
</style>
