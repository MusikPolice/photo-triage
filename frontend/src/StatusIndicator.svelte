<script lang="ts">
  import type { ActivityFeed } from "./lib/activity.svelte";
  import { workerLabel } from "./lib/format";
  import { ROUTE_HREF } from "./lib/route";

  let { feed }: { feed: ActivityFeed } = $props();

  const label = $derived(workerLabel(feed.activity?.worker ?? null, feed.connection, new Date()));
</script>

<a class="status {label.tone}" href={ROUTE_HREF.activity} title="Activity">
  <span class="dot" aria-hidden="true"></span>
  {label.text}
</a>

<style>
  .status {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    color: inherit;
    font-size: 0.875rem;
    text-decoration: none;
    white-space: nowrap;
  }

  .status:hover {
    background: var(--bg);
  }

  .dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--tone);
  }

  .ok {
    --tone: var(--ok);
  }

  .idle {
    --tone: var(--idle);
  }

  .alert {
    --tone: var(--alert);
  }
</style>
