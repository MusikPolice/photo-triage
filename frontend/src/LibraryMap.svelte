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
  import {
    COLOUR_BY,
    COLOUR_BY_LABELS,
    legend,
    pointColours,
    type ColourBy,
    type Rgb,
    type Theme,
  } from "./lib/map-colours";
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

  /** Remembered per browser, as a convenience: it's fine for it to be forgotten. */
  const COLOUR_BY_KEY = "photo-triage.map.colour-by";

  function remembered(): ColourBy {
    try {
      const stored = localStorage.getItem(COLOUR_BY_KEY);
      return COLOUR_BY.find((by) => by === stored) ?? "none";
    } catch {
      return "none";
    }
  }

  let colourBy = $state<ColourBy>(remembered());
  $effect(() => {
    try {
      localStorage.setItem(COLOUR_BY_KEY, colourBy);
    } catch {
      // Private browsing, or storage blocked: it just isn't remembered.
    }
  });

  const darkQuery = matchMedia("(prefers-color-scheme: dark)");
  let theme = $state<Theme>(darkQuery.matches ? "dark" : "light");
  $effect(() => {
    const follow = (event: MediaQueryListEvent) => {
      theme = event.matches ? "dark" : "light";
    };
    darkQuery.addEventListener("change", follow);
    return () => {
      darkQuery.removeEventListener("change", follow);
    };
  });

  const shownLegend = $derived(legend(points, colourBy, theme));

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

  /** The plain dots' colour, `--map-dot`, which follows the theme. */
  function plainColour(element: Element): Rgb {
    const css = getComputedStyle(element).getPropertyValue("--map-dot").trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(css)?.[1] ?? "3b82f6";
    return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)) as unknown as Rgb;
  }

  function layer(shown: MapPoints, colours: Uint8Array) {
    return new ScatterplotLayer({
      id: "points",
      data: {
        length: shown.count,
        attributes: {
          getPosition: { value: shown.positions, size: 2 },
          getFillColor: { value: colours, size: 3, normalized: true },
        },
      },
      radiusUnits: "pixels",
      getRadius: DOT_RADIUS_PX,
    });
  }

  $effect(() => {
    const parent = container;
    const shown = points;
    if (parent === null || shown === null || shown.count === 0) return;
    // Read here, so a change of either redraws the dots.
    const colours = pointColours(shown, colourBy, theme, plainColour(parent));
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
        layers: [layer(shown, colours)],
      });
    } else {
      // New points or colours: keep the view where the user left it.
      deck.setProps({ layers: [layer(shown, colours)] });
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
      get colourBy() {
        return colourBy;
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
  {:else if points !== null}
    <div class="panel">
      <fieldset class="colour-by">
        <legend>Colour</legend>
        {#each COLOUR_BY as by (by)}
          <label class:chosen={colourBy === by}>
            <input type="radio" name="colour-by" value={by} bind:group={colourBy} />
            {COLOUR_BY_LABELS[by]}
          </label>
        {/each}
      </fieldset>
      {#if shownLegend.kind === "swatches"}
        <ul class="key" aria-label="Legend">
          {#each shownLegend.entries as entry (entry.label)}
            <li><span class="swatch" style:background={entry.colour}></span>{entry.label}</li>
          {/each}
        </ul>
      {:else if shownLegend.kind === "ramp"}
        <div class="key" aria-label="Legend">
          <div class="ramp-row">
            <span>{shownLegend.first}</span>
            <span class="ramp" style:background={shownLegend.gradient}></span>
            <span>{shownLegend.last}</span>
          </div>
          {#if shownLegend.unknown !== null}
            <ul>
              <li>
                <span class="swatch" style:background={shownLegend.unknown}></span>Unknown date
              </li>
            </ul>
          {/if}
        </div>
      {/if}
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

  /* Above deck.gl's canvas, which it adds to .map after this. */
  .panel {
    position: absolute;
    top: 0.75rem;
    left: 0.75rem;
    z-index: 1;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.5rem 0.625rem;
    font-size: 0.875rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    box-shadow: 0 1px 3px rgb(0 0 0 / 12%);
  }

  .colour-by {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .colour-by legend {
    float: left;
    margin-right: 0.375rem;
    padding: 0;
    color: var(--muted);
  }

  .colour-by label {
    padding: 0.25rem 0.625rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    cursor: pointer;
  }

  .colour-by label.chosen {
    color: var(--bg);
    background: var(--fg);
    border-color: var(--fg);
  }

  .colour-by label:has(:focus-visible) {
    outline: 2px solid var(--map-dot);
    outline-offset: 2px;
  }

  .colour-by input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .key,
  .key ul {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.875rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .key {
    flex-direction: column;
  }

  ul.key {
    flex-direction: row;
  }

  .key li {
    display: flex;
    align-items: center;
    gap: 0.375rem;
  }

  .swatch {
    width: 0.75rem;
    height: 0.75rem;
    border-radius: 50%;
  }

  .ramp-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-variant-numeric: tabular-nums;
  }

  .ramp {
    flex: 1;
    min-width: 6rem;
    height: 0.625rem;
    border-radius: 999px;
  }

  .note {
    max-width: 30rem;
    margin: 2rem auto;
    padding: 0 1rem;
    color: var(--muted);
    text-align: center;
  }
</style>
