<script lang="ts">
  import Activity from "./Activity.svelte";
  import Home from "./Home.svelte";
  import StatusIndicator from "./StatusIndicator.svelte";
  import Trash from "./Trash.svelte";
  import WriteLog from "./WriteLog.svelte";
  import { ActivityFeed } from "./lib/activity.svelte";
  import { itemFromSearch, withItem } from "./lib/item";
  import { inAppPath, parseRoute, ROUTE_HREF } from "./lib/route";

  let pathname = $state(location.pathname);
  let search = $state(location.search);
  const route = $derived(parseRoute(pathname));
  /** The item open in the lightbox, from `?item=`, so a reload reopens it. */
  const openItem = $derived(route === "home" ? itemFromSearch(search) : null);

  function readLocation(): void {
    pathname = location.pathname;
    search = location.search;
  }

  /** History entries the lightbox pushed, so closing it goes back to the map. */
  const LIGHTBOX_STATE = "lightbox";

  function openLightbox(itemId: number): void {
    // Each item opened from the map is its own entry; one opened from another
    // (the neighbours, later) replaces it, so back always returns to the map.
    const url = location.pathname + withItem(location.search, itemId);
    if (history.state === LIGHTBOX_STATE) history.replaceState(LIGHTBOX_STATE, "", url);
    else history.pushState(LIGHTBOX_STATE, "", url);
    readLocation();
  }

  function closeLightbox(): void {
    if (history.state === LIGHTBOX_STATE) {
      history.back(); // popstate reads the location
    } else {
      // Opened by a link or a reload: there's no map entry behind it to go back to.
      history.replaceState(null, "", location.pathname + withItem(location.search, null));
      readLocation();
    }
  }

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
    readLocation();
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

<svelte:window onclick={followLink} onpopstate={readLocation} />

<header>
  <a class="name" href={ROUTE_HREF.home}>photo-triage</a>
  <nav>
    <a class="link" href={ROUTE_HREF.trash} aria-current={route === "trash" ? "page" : undefined}
      >Trash</a
    >
    <StatusIndicator {feed} />
  </nav>
</header>

<main class:full={route === "home"}>
  {#if route === "activity"}
    <Activity {feed} />
  {:else if route === "writes"}
    <WriteLog />
  {:else if route === "trash"}
    <Trash />
  {:else if route === "home"}
    <Home {feed} {openItem} onOpen={openLightbox} onClose={closeLightbox} />
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

  nav {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .link {
    color: inherit;
    font-size: 0.875rem;
    text-decoration: none;
  }

  .link:hover,
  .link[aria-current="page"] {
    text-decoration: underline;
    text-underline-offset: 0.25em;
  }

  main {
    max-width: 48rem;
    padding: 1rem;
  }

  /* The map fills the screen under the header. */
  main.full {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    max-width: none;
    padding: 0;
  }
</style>
