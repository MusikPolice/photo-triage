/** Reviewing duplicate groups (plan §6.7): the next group, leased to this page, and
 * the decision on it. */

import { postJson } from "./api";
import type { components } from "./api-types";
import { formatBytes, formatDimensions } from "./item";

export type NextGroup = components["schemas"]["NextGroup"];
export type DuplicateGroup = components["schemas"]["Group"];
export type Member = components["schemas"]["Member"];
export type Decided = components["schemas"]["Decided"];
export type Action = components["schemas"]["Action"];

export function getNextGroup(reviewer: string, fetchFn: typeof fetch = fetch): Promise<NextGroup> {
  return postJson<NextGroup>("/duplicates/next", fetchFn, { reviewer });
}

export function decideGroup(
  groupId: number,
  reviewer: string,
  action: Action,
  keep: number[] = [],
  fetchFn: typeof fetch = fetch,
): Promise<Decided> {
  return postJson<Decided>(`/duplicates/${String(groupId)}/decide`, fetchFn, {
    reviewer,
    action,
    keep,
  });
}

const REVIEWER_KEY = "photo-triage.reviewer";

/** This tab's reviewer token, which holds its lease on a group. Kept for the tab, so
 * a reload gets the same group back; a new one if storage is unavailable. */
export function reviewerToken(
  storage: Pick<Storage, "getItem" | "setItem"> | null,
  make: () => string = () => crypto.randomUUID(),
): string {
  try {
    const kept = storage?.getItem(REVIEWER_KEY);
    if (kept != null && /^[A-Za-z0-9-]{8,64}$/.test(kept)) return kept;
    const token = make();
    storage?.setItem(REVIEWER_KEY, token);
    return token;
  } catch {
    return make();
  }
}

/** "12 of 140 groups reviewed" (plan §7). */
export function reviewedSummary(pending: number, total: number): string {
  const groups = total === 1 ? "group" : "groups";
  return `${String(total - pending)} of ${String(total)} ${groups} reviewed`;
}

/** What to compare a member by: "4032 × 3024", "3.1 MB". */
export function memberFacts(member: Member): string[] {
  const facts: string[] = [];
  const size = formatDimensions(member.width, member.height);
  if (size !== null) facts.push(size);
  facts.push(formatBytes(member.file_size_bytes));
  return facts;
}

/** The selection's effect: how many members would go to the trash. */
export function trashCount(keep: ReadonlySet<number>, members: readonly Member[]): number {
  return members.filter((m) => !keep.has(m.id)).length;
}

/** The members kept by default: the recommended keeper. */
export function defaultKeep(members: readonly Member[]): Set<number> {
  const best = members.find((m) => m.rank === 0) ?? members[0];
  return new Set(best === undefined ? [] : [best.id]);
}

/** Let another reviewer have the group: when the page is left or closed. A beacon,
 * so it's sent even as the page goes away. */
export function releaseGroup(
  groupId: number,
  reviewer: string,
  beacon: (url: string, data: Blob) => boolean = (url, data) => navigator.sendBeacon(url, data),
): void {
  const body = new Blob([JSON.stringify({ reviewer })], { type: "application/json" });
  beacon(`/api/duplicates/${String(groupId)}/release`, body);
}
