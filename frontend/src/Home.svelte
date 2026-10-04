<script lang="ts">
  import { getHealth } from "./lib/api";

  const health = getHealth();
</script>

<h1>Your library</h1>
<p>The map, search and review queues will live here.</p>

<p class="status">
  {#await health}
    Checking the API…
  {:then}
    API is up.
  {:catch error}
    API is unreachable: {error instanceof Error ? error.message : String(error)}
  {/await}
</p>

<style>
  h1 {
    margin-top: 0;
    font-size: 1.5rem;
  }

  .status {
    color: var(--muted);
  }
</style>
