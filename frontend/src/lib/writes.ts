/** The write log (plan §6.10): metadata writes, newest first, with the values in the
 * file before and after each. */

import { getJson } from "./api";
import type { components } from "./api-types";

export type WriteLog = components["schemas"]["WriteLog"];
export type LoggedWrite = components["schemas"]["LoggedWrite"];
export type WriteStatus = components["schemas"]["WriteStatus"];

/** Writes the page loads at a time. */
export const WRITE_PAGE_SIZE = 100;

export function getWriteLog(
  status: WriteStatus | null,
  offset = 0,
  limit: number = WRITE_PAGE_SIZE,
  fetchFn: typeof fetch = fetch,
): Promise<WriteLog> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status !== null) params.set("status", status);
  return getJson<WriteLog>(`/metadata-writes?${params.toString()}`, fetchFn);
}

/** The statuses, in the order the filter offers them. */
export const WRITE_STATUSES: readonly WriteStatus[] = ["queued", "applied", "dry_run", "error"];

const STATUS_LABELS: Record<WriteStatus, string> = {
  queued: "Queued",
  applied: "Applied",
  dry_run: "Dry run",
  error: "Error",
};

export function statusLabel(status: WriteStatus): string {
  return STATUS_LABELS[status];
}

const FIELD_LABELS: Record<string, string> = { tags: "Tags", reviewed: "Reviewed" };

export interface FieldChange {
  /** "Tags". */
  label: string;
  /** What the file held, or null if it hasn't been read yet (a queued write). */
  before: string | null;
  /** What it holds after, or will once the write is made. */
  after: string;
}

/** A field's value for people: "beach, café", or "none" for an empty list. */
export function formatValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length === 0 ? "none" : value.map(String).join(", ");
  }
  if (value === null || value === undefined) return "none";
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** Each field the write asked for, with its value before and after. A field the
 * file already had is shown unchanged on both sides. */
export function fieldChanges(write: LoggedWrite): FieldChange[] {
  return Object.keys(write.fields).map((field) => {
    const asked = write.fields[field];
    const read = write.before !== null;
    const before = read && field in (write.before ?? {}) ? write.before?.[field] : asked;
    const after = write.after !== null && field in write.after ? write.after[field] : asked;
    return {
      label: FIELD_LABELS[field] ?? field,
      before: read ? formatValue(before) : null,
      after: formatValue(after),
    };
  });
}
