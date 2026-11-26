<script lang="ts">
  import Lightbox from "./Lightbox.svelte";
  import type { ActivityFeed } from "./lib/activity.svelte";
  import { ROUTE_HREF } from "./lib/route";
  import { restoreItems } from "./lib/trash";

  interface Props {
    feed: ActivityFeed;
    /** The item open in the lightbox, or null. */
    openItem: number | null;
    onOpen: (itemId: number) => void;
    onClose: () => void;
  }

  const { feed, openItem, onOpen, onClose }: Props = $props();

  // deck.gl is most of the app's code, so it loads only with the map.
  const LibraryMap = import("./LibraryMap.svelte").then((module) => module.default);

  /** Items trashed from the lightbox since the map loaded its points: it leaves
   * them out. The next load of the points leaves them out anyway. */
  let hidden = $state.raw<ReadonlySet<number>>(new Set());

  /** How long the toast offers to undo a trash. */
  const TOAST_MS = 10_000;

  /** The toast after a trash: the item it can undo, or just a message. */
  let toast = $state<{ itemId: number | null; text: string } | null>(null);
  let undoing = $state(false);
  let toastTimer: ReturnType<typeof setTimeout> | undefined;

  function showToast(itemId: number | null, text: string): void {
    clearTimeout(toastTimer);
    toast = { itemId, text };
    toastTimer = setTimeout(() => (toast = null), TOAST_MS);
  }

  $effect(() => {
    return () => {
      clearTimeout(toastTimer);
    };
  });

  function trashed(itemId: number): void {
    hidden = new Set([...hidden, itemId]);
    onClose();
    showToast(itemId, "Moved to the trash.");
  }

  async function undo(itemId: number): Promise<void> {
    undoing = true;
    try {
      const result = await restoreItems([itemId]);
      if (result.moved.includes(itemId)) {
        hidden = new Set([...hidden].filter((id) => id !== itemId));
        showToast(null, "Restored.");
      } else {
        showToast(null, `Couldn't restore it: ${result.skipped[0]?.reason ?? "unknown"}.`);
      }
    } catch (error) {
      showToast(null, `Couldn't restore it: ${error instanceof Error ? error.message : ""}`);
    } finally {
      undoing = false;
    }
  }
</script>

{#await LibraryMap then Map}
  <Map
    layoutVersion={feed.activity?.layout_version}
    atlasVersion={feed.activity?.atlas_version}
    focusItem={openItem}
    {onOpen}
    {hidden}
  />
{:catch error}
  <p class="note">
    The map couldn't load: {error instanceof Error ? error.message : String(error)}
  </p>
{/await}

{#if openItem !== null}
  <Lightbox itemId={openItem} {onOpen} {onClose} onTrashed={trashed} />
{/if}

{#if toast !== null}
  <div class="toast" role="status">
    <span>{toast.text}</span>
    {#if toast.itemId !== null}
      {@const itemId = toast.itemId}
      <button type="button" disabled={undoing} onclick={() => undo(itemId)}>Undo</button>
      <a href={ROUTE_HREF.trash}>Trash</a>
    {/if}
  </div>
{/if}

<style>
  .note {
    margin: 2rem 1rem;
    color: var(--muted);
    text-align: center;
  }

  .toast {
    position: fixed;
    bottom: 1.5rem;
    left: 50%;
    z-index: 10;
    display: flex;
    align-items: center;
    gap: 1rem;
    max-width: calc(100vw - 2rem);
    padding: 0.625rem 1rem;
    color: var(--bg);
    background: var(--fg);
    border-radius: 0.5rem;
    box-shadow: 0 4px 16px rgb(0 0 0 / 25%);
    transform: translateX(-50%);
    width: max-content;
    font-size: 0.875rem;
  }

  .toast button {
    padding: 0;
    color: inherit;
    font: inherit;
    font-weight: 600;
    background: none;
    border: 0;
    cursor: pointer;
    text-decoration: underline;
    text-underline-offset: 0.2em;
  }

  .toast button:disabled {
    cursor: default;
    opacity: 0.6;
  }

  .toast a {
    color: inherit;
  }
</style>
