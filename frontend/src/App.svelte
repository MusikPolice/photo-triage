<script lang="ts">
  import Activity from "./Activity.svelte";
  import Home from "./Home.svelte";
  import StatusIndicator from "./StatusIndicator.svelte";
  import { ActivityFeed } from "./lib/activity.svelte";
  import { inAppPath, parseRoute, ROUTE_HREF } from "./lib/route";

  let pathname = $state(location.pathname);
  const route = $derived(parseRoute(pathname));

  /** Follow links within the app without reloading it. */
  function followLink(event: MouseEvent): void {
    const link = event.target instanceof Element ? event.target.closest("a") : null;
    if (link === null) return;
    const path = inAppPath({ ...pick(event), link }, location.origin);
    if (path === null) return;
    event.preventDefault();
    if (path !== location.pathname + location.search + location.hash) {
      history.pushState(null, "", path);
    }
    pathname = location.pathname;
  }

  function pick(event: MouseEvent) {
    const { button, metaKey, ctrlKey, shiftKey, altKey, defaultPrevented } = event;
    return { button, metaKey, ctrlKey, shiftKey, altKey, defaultPrevented };
  }

  // One feed for the whole app, since the header shows it on every view.
  const feed = new ActivityFeed();
  $effect(() => {
    feed.start();
    return () => {
      feed.stop();
    };
  });
</script>

<svelte:window
  onclick={followLink}
  onpopstate={() => {
    pathname = location.pathname;
  }}
/>

<header>
  <a class="name" href={ROUTE_HREF.home}>photo-triage</a>
  <StatusIndicator {feed} />
</header>

<main>
  {#if route === "activity"}
    <Activity {feed} />
  {:else if route === "home"}
    <Home />
  {:else}
    <h1>No page here</h1>
    <p><a href={ROUTE_HREF.home}>Back to your library</a></p>
  {/if}
</main>

<style>
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: var(--header-bg);
    border-bottom: 1px solid var(--border);
  }

  .name {
    color: inherit;
    font-weight: 600;
    text-decoration: none;
  }

  main {
    max-width: 48rem;
    padding: 1rem;
  }
</style>
