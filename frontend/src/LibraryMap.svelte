<script lang="ts">
  /* The map of the library (plan §6.4): every laid-out item as a dot, with deck.gl,
   * and zoomed in, as its thumbnail from the atlas sheets. It pans and zooms with the
   * mouse, the trackpad and touch, and reloads its points and atlas manifest when the
   * activity feed announces a new layout or set of sheets. A search highlights its
   * matches in place: they keep their colour, with a ring, and the rest dim. A
   * narrowed date range dims what's outside it, stacking with a search. */
  import {
    Deck,
    LinearInterpolator,
    OrthographicView,
    type Layer,
    type OrthographicViewState,
  } from "@deck.gl/core";
  import { IconLayer, ScatterplotLayer } from "@deck.gl/layers";
  import { untrack } from "svelte";

  import {
    atlasIndex,
    capacity,
    fetchAtlas,
    nextDetail,
    sheetGroups,
    sheetsFor,
    sheetUrl,
    thumbnailSize,
    type Atlas,
    type Detail,
    type SheetGroup,
  } from "./lib/map-atlas";
  import {
    fetchPoints,
    fitView,
    grown,
    idsWithin,
    indicesWithin,
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
  import DateRangeSlider from "./DateRangeSlider.svelte";
  import {
    dateMask,
    formatMonth,
    monthSpan,
    narrowed,
    shown as shownBy,
    type MonthRange,
  } from "./lib/date-filter";
  import { FrameMeter, wantsFrameRate, type FrameRate } from "./lib/frame-rate";
  import {
    highlight,
    matchCount,
    searchItems,
    shade,
    thumbnailTint,
    type SearchResults,
  } from "./lib/search";
  import { ROUTE_HREF } from "./lib/route";

  interface Props {
    /** The layout version from the activity feed, or null before the first fit or
     * before the feed's first update. */
    layoutVersion: number | null | undefined;
    /** The newest atlas set's layout version, likewise. */
    atlasVersion: number | null | undefined;
    /** The item open in the lightbox, or null: the map flies to it, so it's where
     * the walk through similar items has got to when the lightbox closes. */
    focusItem: number | null;
    /** Opens an item, tapped or clicked on the map, in the lightbox. */
    onOpen: (itemId: number) => void;
  }

  const { layoutVersion, atlasVersion, focusItem, onOpen }: Props = $props();

  let container = $state<HTMLDivElement | null>(null);
  let points = $state.raw<MapPoints | null>(null);
  let failure = $state<string | null>(null);
  let loading: Promise<void> | null = null;
  let atlas = $state.raw<Atlas | null>(null);
  let loadingAtlas: Promise<void> | null = null;
  /** Dots or thumbnails, from how many items are in view. */
  let detail = $state<Detail>("dots");
  /** The sheets drawn, in thumbnail mode: those holding items in or near the view. */
  let sheets = $state.raw<number[]>([]);

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

  /** What's typed in the search box. */
  let query = $state("");
  /** The last search's results, or null with no search. */
  let results = $state.raw<SearchResults | null>(null);
  let searching = $state(false);
  let searchError = $state<string | null>(null);
  /** Counts searches, so a slow answer to an earlier one is ignored. */
  let searchSeq = 0;

  /** The search's matches among the points on the map, or null with no search. */
  const searchMatches = $derived(
    points !== null && results !== null ? highlight(points, results) : null,
  );

  /** The months the library's capture dates span, or null if none has a date. */
  const span = $derived(points === null ? null : monthSpan(points.days));
  /** The date range chosen, or null for every date. */
  let dateRange = $state.raw<MonthRange | null>(null);
  const datesNarrowed = $derived(narrowed(dateRange, span));

  function chooseDates(range: MonthRange): void {
    dateRange = range;
  }
  /** The range the slider shows: the chosen one within the span. */
  const sliderRange = $derived.by((): MonthRange | null => {
    if (span === null) return null;
    if (dateRange === null) return span;
    const from = Math.min(Math.max(dateRange.from, span.from), span.to);
    return { from, to: Math.min(Math.max(dateRange.to, from), span.to) };
  });

  /** What's highlighted, from the search and the date range, or null with neither. */
  const highlighted = $derived(
    points === null ? null : shownBy(points, searchMatches, dateMask(points, sliderRange, span)),
  );

  async function runSearch(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const text = query.trim();
    if (text === "") {
      clearSearch();
      return;
    }
    const seq = ++searchSeq;
    searching = true;
    searchError = null;
    try {
      const found = await searchItems(text);
      if (seq === searchSeq) results = found;
    } catch (error) {
      if (seq === searchSeq) {
        results = null;
        searchError = error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (seq === searchSeq) searching = false;
    }
  }

  function clearSearch(): void {
    searchSeq++;
    query = "";
    results = null;
    searching = false;
    searchError = null;
  }

  function onQueryInput(): void {
    // The box's own clear button, or Escape, empties it: that ends the search.
    if (query.trim() === "" && (results !== null || searchError !== null)) clearSearch();
  }

  function flyToBest(): void {
    const best = highlighted?.matches === null ? undefined : highlighted?.ids[0];
    if (points !== null && best !== undefined) flyTo(points, best);
  }

  /** `?fps`: a frame-rate readout, for trying the map on a phone (#119). */
  const showFrameRate = wantsFrameRate(location.search);
  let frameRate = $state<FrameRate | null>(null);
  $effect(() => {
    if (!showFrameRate) return;
    const meter = new FrameMeter();
    let request = 0;
    const tick = (nowMs: number) => {
      const reading = meter.frame(nowMs);
      if (reading !== null) frameRate = reading;
      request = requestAnimationFrame(tick);
    };
    request = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(request);
    };
  });

  const DOT_RADIUS_PX = 2.5;
  /** A match's ring, around its dot. */
  const RING_PX = 1.5;
  /** How near a tap or click must be to an item to open it: a dot is far smaller
   * than a fingertip. */
  const PICKING_RADIUS_PX = 10;
  const MAX_ZOOM = 20;
  /** How long flying to an item takes. */
  const FLY_MS = 600;
  /** The zoom range, set by the first fit. */
  let minZoom = 0;

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

  function loadAtlas(): void {
    loadingAtlas ??= fetchAtlas()
      .then(
        (loaded) => {
          atlas = loaded;
        },
        (error: unknown) => {
          // The map still works as dots.
          console.warn("The map's thumbnails couldn't load", error);
        },
      )
      .finally(() => {
        loadingAtlas = null;
      });
  }

  // Likewise for the atlas manifest, when a new set of sheets is announced.
  $effect(() => {
    if (atlas === null || (atlasVersion != null && atlasVersion !== atlas.version)) loadAtlas();
  });

  /** Which points are on which sheet, for the current points and atlas. */
  const groups = $derived.by(() => {
    if (points === null || atlas === null) return null;
    const index = atlasIndex(points, atlas);
    return { atlas, index, bySheet: sheetGroups(atlas, index) };
  });

  /** How far beyond the view to load sheets, as a fraction of its size, so a pan
   * finds the next sheet already there. */
  const PRELOAD_MARGIN = 0.25;

  type Groups = NonNullable<typeof groups>;

  /** Work out dots or thumbnails, and which sheets, for the current view. */
  function updateDetail(current: Groups | null = groups): void {
    if (points === null || view === null || container === null) return;
    const { clientWidth: width, clientHeight: height } = container;
    const bounds = viewBounds(view, width, height);
    const next = nextDetail(detail, indicesWithin(points, bounds).length, capacity(width, height));
    if (next !== detail) detail = next;
    const near =
      next === "thumbnails" && current !== null
        ? sheetsFor(
            indicesWithin(points, grown(bounds, PRELOAD_MARGIN)),
            current.atlas,
            current.index,
          )
        : [];
    if (near.join() !== sheets.join()) sheets = near;
  }

  // Again when new points or a new atlas arrive, without waiting for the view to move.
  $effect(() => {
    const current = groups;
    untrack(() => {
      updateDetail(current);
    });
  });

  /** A colour from the theme: `--map-dot` for the plain dots, `--fg` for rings. */
  function cssColour(element: Element, property: string): Rgb {
    const css = getComputedStyle(element).getPropertyValue(property).trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(css)?.[1] ?? "3b82f6";
    return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)) as unknown as Rgb;
  }

  function dotsLayer(shown: MapPoints, colours: Uint8Array) {
    return new ScatterplotLayer({
      id: "points",
      data: {
        length: shown.count,
        attributes: {
          getPosition: { value: shown.positions, size: 2 },
          getFillColor: { value: colours, size: 4, normalized: true },
        },
      },
      radiusUnits: "pixels",
      getRadius: DOT_RADIUS_PX,
      pickable: true,
    });
  }

  /** The matches again, over the dimmed dots, each in its colour with a ring. */
  function matchesLayer(shown: MapPoints, indices: number[], colours: Uint8Array, ring: Rgb) {
    return new ScatterplotLayer<number>({
      id: "matches",
      data: indices,
      getPosition: (i) => [shown.positions[2 * i] ?? 0, shown.positions[2 * i + 1] ?? 0],
      getFillColor: (i) => [colours[4 * i] ?? 0, colours[4 * i + 1] ?? 0, colours[4 * i + 2] ?? 0],
      getLineColor: ring,
      radiusUnits: "pixels",
      getRadius: DOT_RADIUS_PX + RING_PX,
      stroked: true,
      lineWidthUnits: "pixels",
      getLineWidth: RING_PX,
      pickable: true,
    });
  }

  /** One sheet's thumbnails, over the dots: an item with no thumbnail stays a dot.
   * With a search, those that don't match are dimmed. */
  function sheetLayer(
    shown: MapPoints,
    version: number,
    group: SheetGroup,
    mask: Uint8Array | null,
  ) {
    return new IconLayer<number>({
      // A new set gets new layers, and so new textures.
      id: `thumbnails-${String(version)}-${String(group.sheet)}`,
      data: group.points,
      iconAtlas: sheetUrl(version, group.sheet),
      iconMapping: group.mapping,
      getIcon: (i) => String(i),
      getPosition: (i) => [shown.positions[2 * i] ?? 0, shown.positions[2 * i + 1] ?? 0],
      getSize: (i) => {
        const rect = group.mapping[String(i)];
        return rect === undefined ? 0 : thumbnailSize(rect);
      },
      sizeUnits: "pixels",
      getColor: (i) => thumbnailTint(mask, i),
      updateTriggers: { getColor: mask },
      pickable: true,
    });
  }

  /** The layers last drawn, for the test hook. */
  let drawn: Layer[] = [];

  /** The dots' layer, rebuilt only when the points or their colours change. */
  let dots: ScatterplotLayer | null = null;
  /** The matches' layer, with a search. */
  let matches: ScatterplotLayer<number> | null = null;

  $effect(() => {
    const parent = container;
    const shown = points;
    if (parent === null || shown === null || shown.count === 0) return;
    // Read here, so a change of any redraws the dots.
    const found = highlighted;
    const colours = shade(
      pointColours(shown, colourBy, theme, cssColour(parent, "--map-dot")),
      found?.mask ?? null,
    );
    dots = dotsLayer(shown, colours);
    matches =
      found?.matches == null
        ? null
        : matchesLayer(shown, found.matches, colours, cssColour(parent, "--fg"));
    if (deck === null) {
      const { width, height } = parent.getBoundingClientRect();
      view = fitView(shown.positions, width, height);
      deck = new Deck<OrthographicView>({
        parent,
        views: new OrthographicView({ id: "map" }),
        initialViewState: { ...view, minZoom: (minZoom = view.zoom - 2), maxZoom: MAX_ZOOM },
        controller: { dragRotate: false, inertia: true },
        pickingRadius: PICKING_RADIUS_PX,
        getCursor: ({ isDragging, isHovering }) =>
          isDragging ? "grabbing" : isHovering ? "pointer" : "grab",
        onClick: ({ index, object }) => {
          // A thumbnail's object is its point's index (its index is within its
          // sheet); a dot has no object, and its index is the point's.
          const i = typeof object === "number" ? object : index;
          const id = i >= 0 ? points?.ids[i] : undefined;
          if (id !== undefined) onOpen(id);
        },
        onViewStateChange: ({ viewState }) => {
          const state = viewState as OrthographicViewState;
          view = {
            target: [state.target?.[0] ?? 0, state.target?.[1] ?? 0],
            zoom: Number(state.zoom),
          };
          updateDetail();
        },
      });
    }
    // New points or colours keep the view where the user left it. Untracked, so
    // the thumbnails coming and going don't recolour every dot.
    untrack(() => {
      updateDetail();
      draw(detail, sheets, groups);
    });
  });

  // The thumbnails, as they come and go with the view.
  $effect(() => {
    draw(detail, sheets, groups);
  });

  // The open item, at the centre, at the current zoom.
  $effect(() => {
    const id = focusItem;
    const shown = points;
    if (id === null || shown === null) return;
    untrack(() => {
      flyTo(shown, id);
    });
  });

  function flyTo(shown: MapPoints, itemId: number): void {
    if (deck === null || view === null) return;
    const i = shown.ids.indexOf(itemId);
    if (i < 0) return;
    const target: [number, number] = [shown.positions[2 * i] ?? 0, shown.positions[2 * i + 1] ?? 0];
    deck.setProps({
      initialViewState: {
        target,
        zoom: view.zoom,
        minZoom,
        maxZoom: MAX_ZOOM,
        transitionDuration: FLY_MS,
        transitionInterpolator: new LinearInterpolator(["target"]),
      },
    });
  }

  function draw(mode: Detail, shownSheets: number[], current: Groups | null): void {
    const shown = points;
    if (deck === null || dots === null || shown === null) return;
    const layers: Layer[] = [dots];
    if (matches !== null) layers.push(matches);
    if (mode === "thumbnails" && current !== null) {
      const mask = untrack(() => highlighted?.mask ?? null);
      for (const sheet of shownSheets) {
        const group = current.bySheet.get(sheet);
        if (group !== undefined) layers.push(sheetLayer(shown, current.atlas.version, group, mask));
      }
    }
    drawn = layers;
    deck.setProps({ layers });
  }

  $effect(() => {
    return () => {
      deck?.finalize();
      deck = null;
    };
  });

  // A hook for the end-to-end tests (dev-reference §3), which check the map's
  // state rather than its pixels.
  $effect(() => {
    const hook = {
      get version() {
        return points?.version ?? null;
      },
      get colourBy() {
        return colourBy;
      },
      /** The items a search highlights, best first: none without a search. */
      highlightedIds(): number[] {
        return highlighted?.ids ?? [];
      },
      get zoom() {
        return view?.zoom ?? null;
      },
      get atlasVersion() {
        return atlas?.version ?? null;
      },
      /** "dots" or "thumbnails". */
      get detail() {
        return detail;
      },
      /** The atlas sheets drawn, by number. */
      get sheets() {
        return detail === "thumbnails" ? sheets : [];
      },
      /** Whether every layer drawn has what it draws: a sheet's image loads after
       * its layer is made, and until then its thumbnails aren't drawn or tappable. */
      get loaded() {
        return drawn.length > 0 && drawn.every((layer) => layer.isLoaded);
      },
      /** Where an item is on the page, in CSS pixels from the viewport's top left,
       * or null if it isn't on the map. */
      pagePosition(itemId: number): [number, number] | null {
        if (points === null || view === null || container === null) return null;
        const i = points.ids.indexOf(itemId);
        if (i < 0) return null;
        const box = container.getBoundingClientRect();
        const scale = 2 ** view.zoom;
        const x = (points.positions[2 * i] ?? 0) - view.target[0];
        const y = (points.positions[2 * i + 1] ?? 0) - view.target[1];
        return [box.left + box.width / 2 + x * scale, box.top + box.height / 2 + y * scale];
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
      <form class="search" role="search" onsubmit={runSearch}>
        <input
          type="search"
          name="q"
          placeholder="Search, e.g. “the beach”"
          aria-label="Search the library"
          enterkeyhint="search"
          autocomplete="off"
          bind:value={query}
          oninput={onQueryInput}
        />
        <button type="submit" disabled={searching}>Search</button>
      </form>
      {#if searching}
        <p class="search-status" role="status">Searching…</p>
      {:else if searchError !== null}
        <p class="search-status error" role="alert">{searchError}</p>
      {:else if results !== null && highlighted !== null}
        <p class="search-status" role="status">
          <span class="count">{matchCount(highlighted.ids.length)}</span>
          {#if highlighted.ids.length > 0}
            <button type="button" class="link" onclick={flyToBest}>Fly to best match</button>
          {/if}
          <button type="button" class="link" onclick={clearSearch}>Clear</button>
        </p>
      {/if}
      {#if span !== null && sliderRange !== null && span.to > span.from}
        <div class="dates">
          <p class="dates-row">
            <span class="dates-label">Dates</span>
            <span class="dates-range" aria-live="polite">
              {formatMonth(sliderRange.from)} – {formatMonth(sliderRange.to)}
              {#if datesNarrowed && highlighted !== null && results === null}
                · {highlighted.ids.length.toLocaleString()} items
              {/if}
            </span>
            {#if datesNarrowed}
              <button
                type="button"
                class="link"
                onclick={() => {
                  dateRange = null;
                }}>All dates</button
              >
            {/if}
          </p>
          <DateRangeSlider {span} range={sliderRange} onChange={chooseDates} />
        </div>
      {/if}
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
  {#if showFrameRate}
    <output class="frame-rate" aria-label="Frame rate">
      {frameRate === null
        ? "… fps"
        : `${String(frameRate.fps)} fps · worst ${String(Math.round(frameRate.worstMs))} ms`}
      · {points?.count ?? 0} items
    </output>
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

  .search {
    display: flex;
    gap: 0.375rem;
  }

  .search input {
    flex: 1;
    min-width: 0;
    padding: 0.375rem 0.625rem;
    font: inherit;
    color: var(--fg);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 0.375rem;
  }

  .search button {
    padding: 0.375rem 0.75rem;
    font: inherit;
    color: var(--bg);
    background: var(--fg);
    border: 1px solid var(--fg);
    border-radius: 0.375rem;
    cursor: pointer;
  }

  .search button:disabled {
    opacity: 0.6;
    cursor: progress;
  }

  .dates-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 0.625rem;
    margin: 0;
  }

  .dates-label {
    color: var(--muted);
  }

  .dates-range {
    font-variant-numeric: tabular-nums;
  }

  .search-status {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 0.75rem;
    margin: 0;
  }

  .search-status .count {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .search-status.error {
    color: var(--alert);
  }

  .link {
    padding: 0;
    font: inherit;
    color: var(--fg);
    text-decoration: underline;
    background: none;
    border: 0;
    cursor: pointer;
  }

  .frame-rate {
    position: absolute;
    right: 0.75rem;
    bottom: 0.75rem;
    z-index: 1;
    padding: 0.25rem 0.5rem;
    font:
      0.8125rem/1.4 ui-monospace,
      monospace;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 0.375rem;
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
