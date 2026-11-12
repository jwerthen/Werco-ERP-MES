/**
 * Dashboard edit mode: the pure draft + history + drag-session reducer.
 *
 * No React, no DOM, no dnd-kit. `useLayoutEditor` wraps it with the server verbs, focus and
 * announcements; this module only decides what the draft is.
 *
 * - `apply` records one Undo step per REAL change: the draft operations in
 *   `dashboardLayout.ts` return the same object for a no-op, so `next === draft` means
 *   "nothing happened" and no history is written. An `apply` with the SAME `coalesceKey` as
 *   the last step folds into that step (the width radios: arrowing Third → Full width in one
 *   Move-menu visit is one Undo step, labelled with the final width); folding back to the
 *   draft the step started from drops the step.
 * - A pointer drag is a SESSION. While it runs, `session.order` is a render-only order for
 *   the dragged widget's zone (the page renders `applySession(draft, session)`, so the real
 *   CSS grid shows the landing slot). `sessionEnd` commits it as ONE Undo step labelled
 *   "moved <title>" — and only when the order actually changed; `sessionCancel` drops it
 *   without touching the draft.
 * - `replace` (a reload of the saved layout) starts over: new draft, no history, no session.
 */
import { withZoneOrder, zoneIds, type EditableZone, type LayoutDraft } from '../dashboardLayout';

/** Undo depth. Older steps fall off the front. */
export const HISTORY_LIMIT = 50;

export interface HistoryEntry {
  /** The draft BEFORE the step (what Undo restores). */
  draft: LayoutDraft;
  /** What the step did, for "Undone: {label}." — e.g. "moved Capacity Overview". */
  label: string;
  /** A later `apply` with the same key folds into this step instead of adding one. */
  coalesceKey?: string;
}

export interface DragSession {
  zone: EditableZone;
  activeId: string;
  /** The zone's order when the drag started (what a cancel glides back to). */
  startOrder: readonly string[];
  /** The live, render-only order (same members as `startOrder`). */
  order: readonly string[];
}

export interface EditorCore {
  draft: LayoutDraft;
  /** Oldest first; at most HISTORY_LIMIT entries. */
  past: readonly HistoryEntry[];
  session: DragSession | null;
}

export type EditorAction =
  | {
      type: 'apply';
      /** The new draft, or an updater applied to the CURRENT draft (safe against stale closures). */
      next: LayoutDraft | ((draft: LayoutDraft) => LayoutDraft);
      label: string;
      /** Fold into the last step when it carries the same key (one step per Move-menu visit). */
      coalesceKey?: string;
    }
  | { type: 'sessionStart'; zone: EditableZone; activeId: string }
  | { type: 'sessionReorder'; zone: EditableZone; order: readonly string[] }
  | { type: 'sessionEnd'; title: string }
  | { type: 'sessionCancel' }
  | { type: 'undo' }
  | { type: 'replace'; draft: LayoutDraft };

/** The history label for a move (pointer, keyboard or menu). */
export function movedLabel(title: string): string {
  return `moved ${title}`;
}

export function initialEditorCore(draft: LayoutDraft): EditorCore {
  return { draft, past: [], session: null };
}

/**
 * What the page renders while a session runs: the draft with ONLY the session's zone put in
 * the live order. The same draft object when there is no session or the order is unchanged.
 */
export function applySession(draft: LayoutDraft, session: DragSession | null): LayoutDraft {
  if (!session) return draft;
  return withZoneOrder(draft, session.zone, session.order);
}

/** The label Undo would announce, or null when there is nothing to undo. */
export function undoLabel(core: EditorCore): string | null {
  return core.past.length > 0 ? core.past[core.past.length - 1].label : null;
}

function sameOrder(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

function sameMembers(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const members = new Set(a);
  return members.size === a.length && new Set(b).size === b.length && b.every(id => members.has(id));
}

function sameDraft(a: LayoutDraft, b: LayoutDraft): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function pushHistory(core: EditorCore, next: LayoutDraft, label: string, coalesceKey?: string): EditorCore {
  const entry: HistoryEntry =
    coalesceKey === undefined ? { draft: core.draft, label } : { draft: core.draft, label, coalesceKey };
  const past = [...core.past, entry];
  return {
    ...core,
    draft: next,
    past: past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past,
  };
}

export function editorReducer(core: EditorCore, action: EditorAction): EditorCore {
  switch (action.type) {
    case 'apply': {
      const next = typeof action.next === 'function' ? action.next(core.draft) : action.next;
      if (next === core.draft) return core;
      const last = core.past.length > 0 ? core.past[core.past.length - 1] : null;
      if (action.coalesceKey !== undefined && last !== null && last.coalesceKey === action.coalesceKey) {
        const earlier = core.past.slice(0, -1);
        // Back where the step started: the step is gone, not "undo to the same draft".
        if (sameDraft(next, last.draft)) return { ...core, draft: last.draft, past: earlier };
        return { ...core, draft: next, past: [...earlier, { ...last, label: action.label }] };
      }
      return pushHistory(core, next, action.label, action.coalesceKey);
    }
    case 'sessionStart': {
      const startOrder = zoneIds(core.draft, action.zone);
      if (!startOrder.includes(action.activeId)) return core;
      // A new pickup supersedes any session that never ended; its live order is discarded.
      return { ...core, session: { zone: action.zone, activeId: action.activeId, startOrder, order: startOrder } };
    }
    case 'sessionReorder': {
      const session = core.session;
      if (!session || session.zone !== action.zone) return core;
      if (sameOrder(session.order, action.order) || !sameMembers(session.startOrder, action.order)) return core;
      return { ...core, session: { ...session, order: [...action.order] } };
    }
    case 'sessionEnd': {
      const session = core.session;
      if (!session) return core;
      const next = withZoneOrder(core.draft, session.zone, session.order);
      if (next === core.draft) return { ...core, session: null };
      return { ...pushHistory(core, next, movedLabel(action.title)), session: null };
    }
    case 'sessionCancel':
      return core.session ? { ...core, session: null } : core;
    case 'undo': {
      if (core.past.length === 0) return core;
      const entry = core.past[core.past.length - 1];
      return { draft: entry.draft, past: core.past.slice(0, -1), session: null };
    }
    case 'replace':
      return initialEditorCore(action.draft);
    default:
      return core;
  }
}
