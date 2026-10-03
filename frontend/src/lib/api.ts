/** A thin client for the backend's `/api` routes. */

export class ApiError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
  ) {
    super(`GET ${path} failed with HTTP ${String(status)}`);
  }
}

export async function getJson<T>(path: string, fetchFn: typeof fetch = fetch): Promise<T> {
  const response = await fetchFn(`/api${path}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new ApiError(path, response.status);
  return (await response.json()) as T;
}

export interface Health {
  status: "ok";
}

export function getHealth(fetchFn: typeof fetch = fetch): Promise<Health> {
  return getJson<Health>("/health", fetchFn);
}
