/** The activity, kept current from `/api/activity` and the event stream (plan §7). */

import { EVENTS_URL, getActivity, setPaused, type Activity, type Stage } from "./api";

/** `connecting` until the first update, `live` while the stream is open, `lost`
 * while it's down and being retried. */
export type Connection = "connecting" | "live" | "lost";

/** The part of `EventSource` this uses, so tests can fake it. */
export interface EventStream {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

export const CLOSED = 2; // EventSource.CLOSED, which isn't defined outside a browser

/** How long to wait before reopening a stream the browser gave up on. The browser
 * retries by itself after a dropped connection, but not after an error response
 * (e.g. the dev server's proxy answering 502 while the API restarts). */
export const RETRY_MS = 5000;

export interface FeedOptions {
  openStream?: (url: string) => EventStream;
  fetchFn?: typeof fetch;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

export class ActivityFeed {
  activity = $state<Activity | null>(null);
  connection = $state<Connection>("connecting");

  #openStream: (url: string) => EventStream;
  #fetchFn: typeof fetch;
  #setTimer: (callback: () => void, ms: number) => unknown;
  #clearTimer: (handle: unknown) => void;
  #stream: EventStream | null = null;
  #retry: unknown = null;
  #streamed = false;

  constructor(options: FeedOptions = {}) {
    this.#openStream = options.openStream ?? ((url) => new EventSource(url));
    this.#fetchFn = options.fetchFn ?? ((...args) => fetch(...args));
    this.#setTimer = options.setTimer ?? ((callback, ms) => setTimeout(callback, ms));
    this.#clearTimer =
      options.clearTimer ??
      ((handle) => {
        clearTimeout(handle as ReturnType<typeof setTimeout>);
      });
  }

  /** Load the activity and follow the stream until `stop`. */
  start(): void {
    getActivity(this.#fetchFn).then(
      (activity) => {
        // The stream's first message is at least as new.
        if (!this.#streamed) this.activity = activity;
      },
      () => {
        if (!this.#streamed) this.connection = "lost";
      },
    );
    this.#connect();
  }

  stop(): void {
    this.#stream?.close();
    this.#stream = null;
    if (this.#retry !== null) this.#clearTimer(this.#retry);
    this.#retry = null;
  }

  /** Pause or resume the worker (`stage` null) or a stage, and show the result
   * without waiting for the stream. */
  async setPaused(paused: boolean, stage: Stage | null): Promise<void> {
    this.activity = await setPaused(paused, stage, this.#fetchFn);
  }

  #connect(): void {
    const stream = this.#openStream(EVENTS_URL);
    this.#stream = stream;
    stream.onopen = () => {
      this.connection = "live";
    };
    stream.onmessage = (event) => {
      this.#streamed = true;
      this.connection = "live";
      this.activity = JSON.parse(event.data) as Activity;
    };
    stream.onerror = () => {
      this.connection = "lost";
      if (stream.readyState !== CLOSED) return; // the browser is retrying
      stream.close();
      this.#retry = this.#setTimer(() => {
        this.#retry = null;
        if (this.#stream === stream) this.#connect();
      }, RETRY_MS);
    };
  }
}
