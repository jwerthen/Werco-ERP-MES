/**
 * Preventive-maintenance display + payload helpers shared by the Maintenance page and its
 * components. The enums mirror backend/app/models/maintenance.py; the server types every one
 * of them, so an unknown value is a 422 rather than a silently stored string.
 */

import type {
  MaintenanceFrequency,
  MaintenancePriority,
  MaintenanceTargetFields,
  MaintenanceType,
} from '../types/maintenance';

export const MAINTENANCE_TYPE_LABELS: Record<MaintenanceType, string> = {
  preventive: 'Preventive',
  predictive: 'Predictive',
  corrective: 'Corrective',
  emergency: 'Emergency',
};

export const MAINTENANCE_PRIORITY_LABELS: Record<MaintenancePriority, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  critical: 'Critical',
};

/** The server's MaintenanceFrequency, in cadence order. There is no "usage based". */
export const MAINTENANCE_FREQUENCY_LABELS: Record<MaintenanceFrequency, string> = {
  daily: 'Daily',
  weekly: 'Weekly',
  biweekly: 'Bi-weekly',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
  semi_annual: 'Semi-annual',
  annual: 'Annual',
  custom: 'Custom (every N days)',
};

export const frequencyText = (frequency: string, frequencyDays?: number | null): string => {
  if (frequency === 'custom') return frequencyDays ? `Every ${frequencyDays} days` : 'Custom';
  return (MAINTENANCE_FREQUENCY_LABELS as Record<string, string>)[frequency] ?? frequency.replace(/_/g, ' ');
};

/** "CNC-01 · Mori Seiki MV-653" for a work center, "FL-01 · Forklift 1" for an asset. */
export const targetText = (row: Partial<MaintenanceTargetFields>): string => {
  const code = row.target_code ?? (row.target_type === 'asset' ? row.asset_tag : null);
  const name = row.target_name ?? (row.target_type === 'asset' ? row.asset_name : row.work_center_name);
  return [code, name].filter(Boolean).join(' · ') || '—';
};

// Spelled out rather than `\s`, which means different characters in JavaScript and in Python; the
// Python twin uses this exact class. Every run of these becomes one space. (Built with `new RegExp`
// because the `u` flag cannot be written as a literal under the es5 type-check target; `String.raw`
// keeps the escapes byte-for-byte those of the Python source.)
const WHITESPACE = String.raw`\t\n\v\f\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff`;
const WHITESPACE_RUN = new RegExp(`[${WHITESPACE}]+`, 'gu');
// The same class at the ends of a whole stored checklist, stripped before it is parsed, as the server
// does (`trim()` and Python's `strip()` strip different sets).
const WHITESPACE_EDGES = new RegExp(`^[${WHITESPACE}]+|[${WHITESPACE}]+$`, 'gu');
// Leading bullets, after the whitespace collapse: a bullet glyph, or a dash / star / en dash / em dash
// FOLLOWED BY A SPACE (or nothing else) — so `-X axis`, `-ve terminal`, `-40 F` and `*CRITICAL*` keep
// their marks — each with the space after it, as many as there are.
const LEADING_BULLETS = new RegExp(
  String.raw`^(?:(?:[\u2022\u25e6\u25aa\u00b7\u2023\u2043]|[-*\u2013\u2014](?= |$)) ?)+`,
  'u'
);
// Only spaces are left at the ends after the collapse; `trim()` would strip a wider, different set.
const END_SPACES = /^ +| +$/g;

/**
 * A checklist item's text as every reader sees it (owner decision 2026-11-20): whitespace runs
 * (non-breaking spaces included) collapsed to one space, the ends trimmed, leading bullet glyphs
 * (`• ◦ ▪ · ‣ ⁃`, or a dash / star / en dash / em dash followed by a space) dropped. Idempotent.
 * Spelling, never meaning: numbering ("1. Check oil"), a sign or emphasis mark written against a word
 * ("-X axis", "*CRITICAL*") and case are kept — case is ignored only when MATCHING
 * ({@link checklistItemKey}).
 *
 * The TypeScript twin of `normalize_checklist_item` in
 * `backend/app/services/maintenance_checklist.py`; the shared fixture
 * `src/utils/__tests__/fixtures/maintenance-checklist-normalization.json` holds the two together
 * (run by Jest and pytest). Change both, and add the case to the fixture, never one side alone.
 */
export const normalizeChecklistItem = (text: unknown): string =>
  String(text ?? '')
    .replace(WHITESPACE_RUN, ' ')
    .replace(END_SPACES, '')
    .replace(LEADING_BULLETS, '')
    .replace(END_SPACES, '');

/**
 * What two checklist items are matched on: the normalized text, case-insensitively
 * (`toLowerCase`, the twin of Python's `str.lower` in `checklist_item_key`). "Check Oil" ticks
 * "check oil"; the first spelling met is the one shown.
 */
export const checklistItemKey = (text: unknown): string => normalizeChecklistItem(text).toLowerCase();

/** Text typed one item per line, as normalized items (blank lines dropped). */
export const checklistLines = (text: string): string[] =>
  text.split(/\r?\n/).map(normalizeChecklistItem).filter(Boolean);

/**
 * A checklist typed one item per line, sent as the JSON array of strings the model stores
 * (each item normalized, blank lines dropped). An empty checklist is sent as null.
 */
export const checklistPayload = (text: string): string | null => {
  const items = checklistLines(text);
  return items.length ? JSON.stringify(items) : null;
};

type RoleUser = { role?: string | null; is_superuser?: boolean | null } | null | undefined;

/** Schedules, work-order create/edit/hold/cancel, assets and the PM import: ADMIN / MANAGER /
 *  SUPERVISOR -- plus PLATFORM_ADMIN, whom the server's `require_role` always admits (a hidden
 *  control the server would accept is a missing feature, not a safeguard). */
export const canPlanMaintenance = (user: RoleUser): boolean =>
  !!user && (!!user.is_superuser || ['admin', 'manager', 'supervisor', 'platform_admin'].includes(user.role ?? ''));

/** Starting / completing a maintenance work order also admits OPERATOR. */
export const canPerformMaintenance = (user: RoleUser): boolean => canPlanMaintenance(user) || user?.role === 'operator';

/** Statuses a work order can be started from (mirrors the start verb). */
export const STARTABLE_STATUSES = new Set(['scheduled', 'overdue', 'on_hold']);

/** Statuses a work order can still be cancelled from (anything not already over). */
export const CANCELLABLE_STATUSES = new Set(['scheduled', 'overdue', 'in_progress', 'on_hold']);

/** Statuses in which a maintenance work order is still open (mirrors the server's `_OPEN_STATUSES`). */
export const OPEN_WO_STATUSES = new Set(['scheduled', 'overdue', 'in_progress', 'on_hold']);

/** One line of a work order's checklist, ticked off at completion. */
export interface ChecklistResultItem {
  item: string;
  done: boolean;
}

/**
 * A work order's `checklist_results`, read tolerantly: the `[{item, done}]` array this page
 * writes, a plain JSON array of strings (a schedule-style checklist), or one item per line. Every
 * item is normalized ({@link normalizeChecklistItem}) and blanks are dropped, as the server's
 * `parse_checklist_results` reads them.
 */
export const parseChecklistResults = (raw?: string | null): ChecklistResultItem[] => {
  const text = (raw ?? '').replace(WHITESPACE_EDGES, '');
  if (!text) return [];
  try {
    const parsed: unknown = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed
        .map(entry => {
          if (entry && typeof entry === 'object') {
            const record = entry as Record<string, unknown>;
            return { item: normalizeChecklistItem(record.item), done: record.done === true };
          }
          return { item: normalizeChecklistItem(entry), done: false };
        })
        .filter(entry => entry.item);
    }
  } catch {
    // Not JSON: one item per line, below.
  }
  return checklistLines(text).map(item => ({ item, done: false }));
};

/**
 * The checklist a work order is completed with: its own items, then — unticked — any item of its
 * PM schedule's checklist it no longer carries. The server judges both lists, so a schedule step
 * dropped at Start PM or by a later edit must be ticked or explained here too.
 *
 * Items match as the server matches them: by {@link checklistItemKey} (normalized,
 * case-insensitive), counted as a MULTISET, so "- Grease  bearing" on the work order answers the
 * schedule's "grease bearing" and is shown once, while a step named twice stays two rows. An added
 * row takes the FIRST spelling met — the work order's own, then the schedule's — as the server
 * names it.
 */
export const completionChecklist = (
  workOrderItems: ChecklistResultItem[],
  scheduleItems: readonly string[] = []
): ChecklistResultItem[] => {
  const carried = new Map<string, number>();
  const spelling = new Map<string, string>();
  for (const entry of workOrderItems) {
    const key = checklistItemKey(entry.item);
    carried.set(key, (carried.get(key) ?? 0) + 1);
    if (!spelling.has(key)) spelling.set(key, entry.item);
  }
  const missing: ChecklistResultItem[] = [];
  for (const text of scheduleItems) {
    const item = normalizeChecklistItem(text);
    if (!item) continue;
    const key = checklistItemKey(item);
    if (!spelling.has(key)) spelling.set(key, item);
    const left = carried.get(key) ?? 0;
    if (left > 0) carried.set(key, left - 1);
    else missing.push({ item: spelling.get(key) ?? item, done: false });
  }
  return [...workOrderItems, ...missing];
};

/** {@link carryChecklistTicks}'s result: the rebuilt checklist and how many of its items are new. */
export interface ChecklistCarryOver {
  checklist: ChecklistResultItem[];
  /**
   * Items of the fresh list no entry of the previous list matched (counted as a multiset) that
   * are still unticked — the new items the technician now has to tick or explain. A new item the
   * server already holds ticked is not counted.
   */
  added: number;
}

/**
 * Re-applies what the technician had ticked to a checklist re-read from the server (after the
 * completion was refused because the list changed underneath the open dialog).
 *
 * Items are matched by {@link checklistItemKey} (normalized, case-insensitive) as a MULTISET, the
 * way the server counts them: two ticked copies of "Grease bearing" before and three copies now
 * leave two ticked and one new; a tick on "grease  bearing" carries to "Grease bearing". Within one text the
 * ticked copies are matched first, so a tick is never lost to an unticked twin. A matched item
 * keeps the dialog's state (the technician's answer wins over the stored one); an unmatched item
 * is new and keeps the server's state (and counts in `added` only while unticked). Items the
 * fresh list no longer carries are dropped.
 */
export const carryChecklistTicks = (
  previous: readonly ChecklistResultItem[],
  fresh: readonly ChecklistResultItem[]
): ChecklistCarryOver => {
  const answered = new Map<string, { ticked: number; unticked: number }>();
  for (const entry of previous) {
    const key = checklistItemKey(entry.item);
    const counts = answered.get(key) ?? { ticked: 0, unticked: 0 };
    if (entry.done) counts.ticked += 1;
    else counts.unticked += 1;
    answered.set(key, counts);
  }
  let added = 0;
  const checklist = fresh.map(entry => {
    const counts = answered.get(checklistItemKey(entry.item));
    if (counts && counts.ticked > 0) {
      counts.ticked -= 1;
      return { item: entry.item, done: true };
    }
    if (counts && counts.unticked > 0) {
      counts.unticked -= 1;
      return { item: entry.item, done: false };
    }
    if (!entry.done) added += 1;
    return { item: entry.item, done: entry.done };
  });
  return { checklist, added };
};

/**
 * The `notes` a completion sends. The dialog opens with the work order's own notes, and the
 * server REPLACES them with whatever is sent: omitted keeps them, a string replaces them, `null`
 * clears them. So notes are sent only when the technician changed them (whitespace at the ends
 * is not a change) — trimmed, or `null` when the field was emptied.
 */
export const completionNotesPayload = (notes: string, openedWith: string): { notes?: string | null } => {
  const text = notes.trim();
  if (text === openedWith.trim()) return {};
  return { notes: text || null };
};

/** The `checklist_results` a work order is opened with: one unticked, normalized item per non-blank line. */
export const checklistResultsPayload = (text: string): string | null => {
  const items = checklistLines(text);
  return items.length ? JSON.stringify(items.map(item => ({ item, done: false }))) : null;
};

/** The open work order (if any) each PM schedule is being worked under, keyed by schedule id. */
export const openWorkOrdersBySchedule = <T extends { schedule_id?: number | null; status: string }>(
  workOrders: T[]
): Map<number, T> => {
  const bySchedule = new Map<number, T>();
  for (const wo of workOrders) {
    if (wo.schedule_id != null && OPEN_WO_STATUSES.has(wo.status) && !bySchedule.has(wo.schedule_id)) {
      bySchedule.set(wo.schedule_id, wo);
    }
  }
  return bySchedule;
};
