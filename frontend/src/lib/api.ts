/** A thin client for the backend's `/api` routes. */

import type { components } from "./api-types";

// Generated from the backend's OpenAPI schema by `scripts/api_types.sh`.
type Schemas = components["schemas"];
export type Health = Schemas["Health"];
export type Activity = Schemas["Activity"];
export type WorkerActivity = Schemas["WorkerActivity"];
export type StageActivity = Schemas["StageActivity"];
export type Stage = Schemas["Stage"];
export type WorkerStatus = Schemas["WorkerStatus"];

/** The Server-Sent Events stream of `Activity` (plan §7 "Live updates"). */
export const EVENTS_URL = "/api/events";

type Method = "GET" | "POST";

export class ApiError extends Error {
  constructor(
    readonly method: Method,
    readonly path: string,
    readonly status: number,
  ) {
    super(`${method} ${path} failed with HTTP ${String(status)}`);
  }
}

async function request<T>(method: Method, path: string, fetchFn: typeof fetch): Promise<T> {
  const response = await fetchFn(`/api${path}`, {
    method,
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new ApiError(method, path, response.status);
  return (await response.json()) as T;
}

export function getJson<T>(path: string, fetchFn: typeof fetch = fetch): Promise<T> {
  return request<T>("GET", path, fetchFn);
}

export function postJson<T>(path: string, fetchFn: typeof fetch = fetch): Promise<T> {
  return request<T>("POST", path, fetchFn);
}

export function getHealth(fetchFn: typeof fetch = fetch): Promise<Health> {
  return getJson<Health>("/health", fetchFn);
}

export function getActivity(fetchFn: typeof fetch = fetch): Promise<Activity> {
  return getJson<Activity>("/activity", fetchFn);
}

/** Pause or resume the whole worker (`stage` null) or one stage. Answers with the
 * activity after the change. */
export function setPaused(
  paused: boolean,
  stage: Stage | null,
  fetchFn: typeof fetch = fetch,
): Promise<Activity> {
  const scope = stage === null ? "/worker" : `/worker/stages/${encodeURIComponent(stage)}`;
  return postJson<Activity>(`${scope}/${paused ? "pause" : "resume"}`, fetchFn);
}
