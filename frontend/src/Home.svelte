<script lang="ts">
  import Lightbox from "./Lightbox.svelte";
  import type { ActivityFeed } from "./lib/activity.svelte";

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
</script>

{#await LibraryMap then Map}
  <Map
    layoutVersion={feed.activity?.layout_version}
    atlasVersion={feed.activity?.atlas_version}
    {onOpen}
  />
{:catch error}
  <p class="note">
    The map couldn't load: {error instanceof Error ? error.message : String(error)}
  </p>
{/await}

{#if openItem !== null}
  <Lightbox itemId={openItem} {onClose} />
{/if}

<style>
  .note {
    margin: 2rem 1rem;
    color: var(--muted);
    text-align: center;
  }
</style>
