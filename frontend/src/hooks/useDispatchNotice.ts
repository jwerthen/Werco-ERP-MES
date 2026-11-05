/**
 * useDispatchNotice — which "the planner changed this machine's order" notices
 * a floor screen should still be showing, and the acknowledgement that hides
 * them (Dispatch Board v2, SPEC §7).
 *
 * The notices ride polls the floor screens already make (CONTRACT §7.3):
 * `dispatch_notice` on the work-center queue, `dispatch_notices` on
 * my-active-job, and `dispatch_notices` on /shop-floor/operations filtered to
 * one work center. The server returns only the NEWEST notice per work center
 * from the last 12 hours, so everything about "have I seen this?" is decided
 * here, on the device.
 *
 * Rules, each load-bearing:
 *
 * - **Ack scope.** A crew station acknowledges per DEVICE per work center (it is
 *   one shared terminal and the board is its identity). Personal screens
 *   (OperatorKiosk's signed-in operator, ShopFloorSimple, ShopFloor) acknowledge
 *   per (USER id, work center): OperatorKiosk is a shared tablet that stops
 *   polling while signed out, so a per-device ack would let operator A's "Got it"
 *   hide the notice from operator B who badges in next.
 * - **Shown while** the newest notice id for the work center is greater than the
 *   acknowledged id. Notice ids increase with every notice (they are event ids),
 *   so "newer" is "larger".
 * - **First-ever sighting.** A scope that has never seen this work center treats
 *   a notice older than 15 minutes as already seen. The age is measured against
 *   the SAME payload's `server_time`, never the device clock — a tablet whose
 *   clock drifted must not resurrect or swallow a notice.
 * - **"Changed N times".** `updates_in_window` is a 12-hour total, not "since you
 *   checked", so it is never shown as such. Instead the device remembers the
 *   DISTINCT notice ids it has itself observed since the last acknowledgement and
 *   reports that count — accurate only while the page is polling, which is why
 *   callers say "since you last checked" only when it is above 1.
 * - **Storage never breaks the page.** Every localStorage touch is wrapped: in
 *   private mode / with blocked site data the acknowledgement falls back to
 *   memory for as long as the page stays mounted.
 * - **No side effects beyond storage.** This hook never navigates, never touches
 *   an idle timer, never shows a toast. A poll that brings a notice only changes
 *   what is rendered.
 * - **Older backend.** Absent fields (`dispatch_notice` / `dispatch_notices`
 *   missing, `moved_up` missing) mean "no notice", never a crash.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DispatchNotice } from '../types';

/** A first sighting older than this (by the payload's `server_time`) counts as already seen. */
export const DISPATCH_NOTICE_FIRST_SIGHTING_MAX_AGE_MS = 15 * 60 * 1000;

/** localStorage key prefix. Versioned so a shape change can start clean. */
export const DISPATCH_NOTICE_STORAGE_PREFIX = 'werco.dispatchNoticeAck.v1';

/** Bound on remembered ids per scope + work center (only the count is ever read). */
const MAX_SEEN_IDS = 50;

/**
 * Whose acknowledgement this is.
 * - `device`: the crew station — one shared terminal, acked per device per work center.
 * - `user`: a personal screen — acked per (user id, work center), so the next
 *   operator on a shared tablet still sees what the previous one dismissed.
 */
export type DispatchNoticeAckScope = { kind: 'device' } | { kind: 'user'; userId: number };

/** The notices from ONE payload, with that payload's own clock. */
export interface DispatchNoticeSource {
  /** Absent / null entries are ignored — an older backend omits the fields. */
  notices?: ReadonlyArray<DispatchNotice | null | undefined> | null;
  /** That SAME payload's `server_time` (UTC `Z`) — the only clock a notice is aged against. */
  serverTime?: string | null;
}

/** A notice the screen should be showing right now. */
export interface ActiveDispatchNotice {
  notice: DispatchNotice;
  /** True when the queue's "run next" job changed (`next_up_before` ≠ `next_up_after`). */
  nextUpChanged: boolean;
  /** Distinct notice ids THIS device observed for this scope + work center since the last acknowledgement (≥ 1). */
  observedChanges: number;
}

export interface UseDispatchNoticeOptions {
  sources: ReadonlyArray<DispatchNoticeSource>;
  /** Null = no identity to key an acknowledgement by yet → nothing is shown. */
  scope: DispatchNoticeAckScope | null;
}

export interface UseDispatchNoticeResult {
  /** Unacknowledged notices, newest first, at most one per work center. */
  active: ActiveDispatchNotice[];
  /** Operation ids that moved up, across every active notice — the "MOVED UP" card chip. */
  movedUpOperationIds: ReadonlySet<number>;
  /** "Got it": hides this notice (and anything older) for this scope + work center. */
  acknowledge: (notice: DispatchNotice) => void;
}

interface AckRecord {
  /** Highest acknowledged notice id; null = sighted but never acknowledged. */
  ackedId: number | null;
  /** Distinct notice ids observed since that acknowledgement. */
  seen: number[];
}

interface LatestEntry {
  notice: DispatchNotice;
  serverTime: string | null;
}

function scopeToken(scope: DispatchNoticeAckScope): string {
  return scope.kind === 'device' ? 'device' : `user:${scope.userId}`;
}

/** Storage key for one scope + work center. Exported for tests. */
export function dispatchNoticeAckKey(scope: DispatchNoticeAckScope, workCenterId: number): string {
  return `${DISPATCH_NOTICE_STORAGE_PREFIX}:${scopeToken(scope)}:wc:${workCenterId}`;
}

/** True when the queue's "run next" job is a different operation after the change. */
export function dispatchNoticeNextUpChanged(notice: Pick<DispatchNotice, 'next_up_before' | 'next_up_after'>): boolean {
  return (notice.next_up_before?.operation_id ?? null) !== (notice.next_up_after?.operation_id ?? null);
}

/** A notice the rest of this module can trust — anything malformed is treated as absent. */
function isUsableNotice(value: unknown): value is DispatchNotice {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<DispatchNotice>;
  return (
    typeof candidate.id === 'number' &&
    Number.isFinite(candidate.id) &&
    typeof candidate.work_center_id === 'number' &&
    Number.isFinite(candidate.work_center_id)
  );
}

function parseRecord(raw: string | null): AckRecord | null {
  if (raw == null) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<AckRecord> | null;
    if (!parsed || typeof parsed !== 'object') return null;
    const ackedId = typeof parsed.ackedId === 'number' && Number.isFinite(parsed.ackedId) ? parsed.ackedId : null;
    const seen = Array.isArray(parsed.seen)
      ? parsed.seen.filter((id): id is number => typeof id === 'number' && Number.isFinite(id))
      : [];
    return { ackedId, seen };
  } catch {
    return null;
  }
}

/**
 * Has this notice aged past the first-sighting window? Measured against the
 * payload's own `server_time`; an unknown age counts as FRESH — hiding a real
 * change from the operator costs more than showing a stale one once.
 */
function olderThanFirstSightingWindow(notice: DispatchNotice, serverTime: string | null): boolean {
  if (!serverTime) return false;
  const serverMs = Date.parse(serverTime);
  const occurredMs = Date.parse(notice.occurred_at);
  if (!Number.isFinite(serverMs) || !Number.isFinite(occurredMs)) return false;
  return serverMs - occurredMs > DISPATCH_NOTICE_FIRST_SIGHTING_MAX_AGE_MS;
}

function changesSinceAck(record: AckRecord): number {
  const since = record.ackedId;
  return new Set(record.seen.filter(id => since == null || id > since)).size;
}

/** Newest notice per work center across every source (deduped by id). */
function latestPerWorkCenter(sources: ReadonlyArray<DispatchNoticeSource>): LatestEntry[] {
  const byWorkCenter = new Map<number, LatestEntry>();
  for (const source of sources) {
    const list = Array.isArray(source?.notices) ? source.notices : [];
    for (const raw of list) {
      if (!isUsableNotice(raw)) continue;
      const current = byWorkCenter.get(raw.work_center_id);
      if (!current || raw.id > current.notice.id) {
        byWorkCenter.set(raw.work_center_id, { notice: raw, serverTime: source.serverTime ?? null });
      }
    }
  }
  return Array.from(byWorkCenter.values()).sort((a, b) => b.notice.id - a.notice.id);
}

export function useDispatchNotice({ sources, scope }: UseDispatchNoticeOptions): UseDispatchNoticeResult {
  const latest = latestPerWorkCenter(sources);
  const latestRef = useRef(latest);
  latestRef.current = latest;

  const scopeKey = scope ? scopeToken(scope) : null;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  // Only what changes the decision: which notice is newest at which machine.
  const signature = latest.map(entry => `${entry.notice.work_center_id}:${entry.notice.id}`).join('|');

  // Fallback store for when localStorage throws (private mode, blocked data).
  // Per mounted page on purpose: no module-level state to leak between screens.
  const memoryRef = useRef(new Map<string, AckRecord>());
  const [records, setRecords] = useState<Record<string, AckRecord>>({});
  const [reloadToken, setReloadToken] = useState(0);

  const readRecord = useCallback((key: string): AckRecord | null => {
    // A value only lands in memory when the storage WRITE failed, so when it is
    // there it is the newest thing this page knows.
    const remembered = memoryRef.current.get(key);
    if (remembered) return remembered;
    try {
      return parseRecord(window.localStorage.getItem(key));
    } catch {
      return null;
    }
  }, []);

  const writeRecord = useCallback((key: string, record: AckRecord) => {
    try {
      window.localStorage.setItem(key, JSON.stringify(record));
      memoryRef.current.delete(key);
    } catch {
      memoryRef.current.set(key, record);
    }
  }, []);

  // Observe: apply the first-sighting rule and remember every distinct id seen.
  // Storage writes only — no navigation, no timers, no toasts.
  useEffect(() => {
    const activeScope = scopeRef.current;
    if (!activeScope) {
      setRecords({});
      return;
    }
    const next: Record<string, AckRecord> = {};
    for (const { notice, serverTime } of latestRef.current) {
      const key = dispatchNoticeAckKey(activeScope, notice.work_center_id);
      let record = readRecord(key);
      if (record == null) {
        record = olderThanFirstSightingWindow(notice, serverTime)
          ? { ackedId: notice.id, seen: [] }
          : { ackedId: null, seen: [notice.id] };
        writeRecord(key, record);
      } else if ((record.ackedId == null || notice.id > record.ackedId) && !record.seen.includes(notice.id)) {
        record = { ackedId: record.ackedId, seen: [...record.seen, notice.id].slice(-MAX_SEEN_IDS) };
        writeRecord(key, record);
      }
      next[key] = record;
    }
    setRecords(next);
  }, [scopeKey, signature, reloadToken, readRecord, writeRecord]);

  // Another tab of the same screen acknowledged: re-read rather than keep showing it.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key == null || event.key.startsWith(DISPATCH_NOTICE_STORAGE_PREFIX)) {
        setReloadToken(token => token + 1);
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const acknowledge = useCallback(
    (notice: DispatchNotice) => {
      const activeScope = scopeRef.current;
      if (!activeScope || !isUsableNotice(notice)) return;
      const key = dispatchNoticeAckKey(activeScope, notice.work_center_id);
      const existing = readRecord(key);
      const ackedId = Math.max(notice.id, existing?.ackedId ?? Number.NEGATIVE_INFINITY);
      const record: AckRecord = { ackedId, seen: (existing?.seen ?? []).filter(id => id > ackedId) };
      writeRecord(key, record);
      setRecords(prev => ({ ...prev, [key]: record }));
    },
    [readRecord, writeRecord]
  );

  // `scopeKey` and `signature` stand in for the refs' content in the deps.
  const active = useMemo<ActiveDispatchNotice[]>(() => {
    const activeScope = scopeRef.current;
    if (!activeScope || scopeKey == null) return [];
    const shown: ActiveDispatchNotice[] = [];
    for (const { notice } of latestRef.current) {
      const record = records[dispatchNoticeAckKey(activeScope, notice.work_center_id)];
      // No record yet = the observe effect has not judged this notice (it may be
      // an old first sighting). Wait one render rather than flash it.
      if (!record) continue;
      if (record.ackedId != null && notice.id <= record.ackedId) continue;
      shown.push({
        notice,
        nextUpChanged: dispatchNoticeNextUpChanged(notice),
        observedChanges: Math.max(1, changesSinceAck(record)),
      });
    }
    return shown;
  }, [scopeKey, signature, records]);

  const movedUpOperationIds = useMemo(() => {
    const ids = new Set<number>();
    for (const { notice } of active) {
      const movedUp = Array.isArray(notice.moved_up) ? notice.moved_up : [];
      for (const id of movedUp) {
        if (typeof id === 'number') ids.add(id);
      }
    }
    return ids;
  }, [active]);

  return { active, movedUpOperationIds, acknowledge };
}
