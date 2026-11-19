<script lang="ts">
  /* The map of the library (plan §6.4): every laid-out item as a dot, with deck.gl.
   * It pans and zooms with the mouse, the trackpad and touch, and reloads its points
   * when the activity feed announces a new layout version. */
  import { Deck, OrthographicView, type OrthographicViewState } from "@deck.gl/core";
  import { ScatterplotLayer } from "@deck.gl/layers";

  import {
    fetchPoints,
    fitView,
    idsWithin,
    viewBounds,
    type MapPoints,
    type MapView,
  } from "./lib/map-points";
  import { ROUTE_HREF } from "./lib/route";

  interface Props {
    /** The layout version from the activity feed, or null before the first fit or
     * before the feed's first update. */
    layoutVersion: number | null | undefined;
  }

  const { layoutVersion }: Props = $props();

  let container = $state<HTMLDivElement | null>(null);
  let points = $state.raw<MapPoints | null>(null);
  let failure = $state<string | null>(null);
  let loading: Promise<void> | null = null;

  /** deck.gl's own state, outside Svelte's: it redraws by itself. */
  let deck: Deck<OrthographicView> | null = null;
  let view: MapView | null = null;

  const DOT_RADIUS_PX = 2.5;
  const MAX_ZOOM = 20;

  function load(): void {
    loading ??= fetchPoints()
      .then(
        (loaded) => {
          points = loaded;
          failure = null;
        },
        (error: unknown) => {
          failure = error instanceof Error ? error.message : String(error);
        },
      )
      .finally(() => {
        loading = null;
      });
  }

  // The first load, and again whenever a newer layout is announced.
  $effect(() => {
    if (points === null || (layoutVersion != null && layoutVersion !== points.version)) load();
  });

  function dotColor(element: Element): [number, number, number] {
    const css = getComputedStyle(element).getPropertyValue("--map-dot").trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(css)?.[1] ?? "3b82f6";
    return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)) as [number, number, number];
  }

  function layer(shown: MapPoints, color: [number, number, number]) {
    return new ScatterplotLayer({
      id: "points",
      data: {
        length: shown.count,
        attributes: { getPosition: { value: shown.positions, size: 2 } },
      },
      getFillColor: color,
      radiusUnits: "pixels",
      getRadius: DOT_RADIUS_PX,
      // The layout is redrawn only when the points change.
      updateTriggers: { getFillColor: color },
    });
  }

  $effect(() => {
    const parent = container;
    const shown = points;
    if (parent === null || shown === null || shown.count === 0) return;
    const color = dotColor(parent);
    if (deck === null) {
      const { width, height } = parent.getBoundingClientRect();
      view = fitView(shown.positions, width, height);
      deck = new Deck<OrthographicView>({
        parent,
        views: new OrthographicView({ id: "map" }),
        initialViewState: { ...view, minZoom: view.zoom - 2, maxZoom: MAX_ZOOM },
        controller: { dragRotate: false, inertia: true },
        onViewStateChange: ({ viewState }) => {
          const state = viewState as OrthographicViewState;
          view = {
            target: [state.target?.[0] ?? 0, state.target?.[1] ?? 0],
            zoom: Number(state.zoom),
          };
        },
        layers: [layer(shown, color)],
      });
    } else {
      // A new layout: keep the view where the user left it.
      deck.setProps({ layers: [layer(shown, color)] });
    }
  });

  $effect(() => {
    return () => {
      deck?.finalize();
      deck = null;
    };
  });

  // A hook for the end-to-end tests (dev-environment §6), which check the map's
  // state rather than its pixels.
  $effect(() => {
    const hook = {
      get version() {
        return points?.version ?? null;
      },
      get zoom() {
        return view?.zoom ?? null;
      },
      visibleIds(): number[] {
        if (points === null || view === null || container === null) return [];
        const { width, height } = container.getBoundingClientRect();
        return idsWithin(points, viewBounds(view, width, height));
      },
    };
    Object.assign(window, { __photoTriageMap: hook });
    return () => {
      Reflect.deleteProperty(window, "__photoTriageMap");
    };
  });
</script>

<div class="map" bind:this={container}>
  {#if failure !== null}
    <p class="note">The map couldn't load: {failure}</p>
  {:else if points !== null && points.count === 0}
    <div class="note">
      <p>The map appears once items are embedded.</p>
      <p>See how far along that is on the <a href={ROUTE_HREF.activity}>Activity page</a>.</p>
    </div>
  {/if}
</div>

<style>
  .map {
    position: relative;
    flex: 1;
    min-height: 0;
    /* deck.gl handles every touch gesture on the map itself. */
    touch-action: none;
    overflow: hidden;
  }

  .note {
    max-width: 30rem;
    margin: 2rem auto;
    padding: 0 1rem;
    color: var(--muted);
    text-align: center;
  }
</style>
