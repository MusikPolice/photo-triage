import { describe, expect, it, vi } from "vitest";

import { ActivityFeed, CLOSED, RETRY_MS, type EventStream } from "../src/lib/activity.svelte";
import type { Activity } from "../src/lib/api";

function activity(status: "running" | "paused", done = 0): Activity {
  return {
    worker: { status, paused: status === "paused", quiet_until_at: null, last_seen_at: null },
    stages: [
      {
        stage: "scan",
        kind: "item",
        paused: false,
        done,
        total: 10,
        pending: 10 - done,
        running: 0,
        errored: 0,
        parked: 0,
      },
    ],
  };
}

class FakeStream implements EventStream {
  readyState = 0;
  closed = false;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: Activity): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }

  fail(readyState: number): void {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }

  close(): void {
    this.closed = true;
  }
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

/** A feed whose fetch answers from `routes` (method and path) and whose streams
 * and timers are recorded. */
function setUp(routes: Record<string, () => Response>) {
  const streams: FakeStream[] = [];
  const timers: (() => void)[] = [];
  const fetchFn = vi.fn<typeof fetch>((input, init) => {
    const route = routes[`${init?.method ?? "GET"} ${urlOf(input)}`];
    return Promise.resolve(route ? route() : new Response(null, { status: 404 }));
  });
  const feed = new ActivityFeed({
    fetchFn,
    openStream: (url) => {
      const stream = new FakeStream(url);
      streams.push(stream);
      return stream;
    },
    setTimer: (callback, ms) => {
      expect(ms).toBe(RETRY_MS);
      timers.push(callback);
      return timers.length;
    },
    clearTimer: () => undefined,
  });
  return { feed, fetchFn, streams, timers };
}

describe("ActivityFeed", () => {
  it("shows /api/activity, then each update from /api/events", async () => {
    const { feed, streams } = setUp({
      "GET /api/activity": () => Response.json(activity("running")),
    });

    feed.start();
    await vi.waitFor(() => {
      expect(feed.activity).toEqual(activity("running"));
    });
    expect(streams.map((s) => s.url)).toEqual(["/api/events"]);

    streams[0]?.send(activity("running", 4));
    expect(feed.activity?.stages[0]?.done).toBe(4);
    expect(feed.connection).toBe("live");
  });

  it("doesn't let the first load replace a newer streamed update", async () => {
    const { feed, streams } = setUp({
      "GET /api/activity": () => Response.json(activity("running", 1)),
    });
    feed.start();
    streams[0]?.send(activity("running", 5)); // before the fetch answers
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(feed.activity?.stages[0]?.done).toBe(5);
  });

  it("is lost if the API can't be reached", async () => {
    const { feed } = setUp({ "GET /api/activity": () => new Response(null, { status: 502 }) });
    feed.start();
    await vi.waitFor(() => {
      expect(feed.connection).toBe("lost");
    });
  });

  it("pauses and resumes, showing the activity the API answers with", async () => {
    const { feed, fetchFn } = setUp({
      "POST /api/worker/pause": () => Response.json(activity("paused")),
      "POST /api/worker/stages/scan/resume": () => Response.json(activity("running")),
    });

    await feed.setPaused(true, null);
    expect(feed.activity?.worker.status).toBe("paused");

    await feed.setPaused(false, "scan");
    expect(feed.activity?.worker.status).toBe("running");
    expect(fetchFn.mock.calls.map(([url, init]) => `${init?.method ?? ""} ${urlOf(url)}`)).toEqual([
      "POST /api/worker/pause",
      "POST /api/worker/stages/scan/resume",
    ]);
  });

  it("lets a failed pause throw, leaving the activity as it was", async () => {
    const { feed } = setUp({});
    await expect(feed.setPaused(true, null)).rejects.toMatchObject({ status: 404 });
    expect(feed.activity).toBeNull();
  });

  it("is lost while the browser retries, and live again when a message comes", () => {
    const { feed, streams, timers } = setUp({});
    feed.start();
    streams[0]?.fail(0); // CONNECTING: the browser reconnects by itself
    expect(feed.connection).toBe("lost");
    expect(timers).toEqual([]);

    streams[0]?.send(activity("running"));
    expect(feed.connection).toBe("live");
  });

  it("reopens a stream the browser gave up on", () => {
    const { feed, streams, timers } = setUp({});
    feed.start();
    streams[0]?.fail(CLOSED);
    expect(streams[0]?.closed).toBe(true);
    expect(timers).toHaveLength(1);

    timers[0]?.();
    expect(streams).toHaveLength(2);
  });

  it("stops following the stream", () => {
    const { feed, streams, timers } = setUp({});
    feed.start();
    streams[0]?.fail(CLOSED);
    feed.stop();
    timers[0]?.();
    expect(streams).toHaveLength(1);
  });
});
