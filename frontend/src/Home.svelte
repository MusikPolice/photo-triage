<script lang="ts">
  import type { ActivityFeed } from "./lib/activity.svelte";

  interface Props {
    feed: ActivityFeed;
  }

  const { feed }: Props = $props();

  // deck.gl is most of the app's code, so it loads only with the map.
  const LibraryMap = import("./LibraryMap.svelte").then((module) => module.default);
</script>

{#await LibraryMap then Map}
  <Map layoutVersion={feed.activity?.layout_version} />
{:catch error}
  <p class="note">
    The map couldn't load: {error instanceof Error ? error.message : String(error)}
  </p>
{/await}

<style>
  .note {
    margin: 2rem 1rem;
    color: var(--muted);
    text-align: center;
  }
</style>
