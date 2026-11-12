/**
 * Dashboard edit mode: the draft, its history and every edit-mode verb, for the page.
 *
 * No dnd-kit here (main bundle). The pure reducer (`editorState.ts`) decides what the draft
 * is; this hook adds the session lifecycle, the non-optimistic server verbs (through the
 * UNCHANGED `useDashboardLayout` result), focus, announcements and the unsaved-changes guard.
 *
 * ## Scope tagging (same render)
 * The edit session is tagged with the session scope it was entered under. `active` is true
 * only while that tag equals the CURRENT scope, so a user / company / read-only switch reads
 * as "not editing" in the very render the scope changes — the previous session's draft is
 * never painted, fetched or saved (mirrors `useDashboardLayout`). An effect then clears it:
 * a dirty draft gets the warning toast, focus that dropped to `<body>` goes to the header
 * Customize button (or `#main-content` while Customize is disabled), and the generation
 * counter is bumped so a late save / reset / reload result is ignored.
 *
 * ## Contract with the page
 * - `onBeforeChange` is called right BEFORE every committed change that moves a cell (the
 *   page passes `useFlipLayout(...).capture`), and never for a no-op.
 * - `announce(message)` writes into the page's polite live region. The page must re-key the
 *   region's child per call so an identical string is spoken again. The gallery's "added"
 *   message is the one exception: `add()` RETURNS it for the gallery's own region (an
 *   `aria-modal` dialog hides outside live regions from VoiceOver).
 * - The header Customize button carries `id={EDIT_IDS.customize}`; the app layout's
 *   `<main id="main-content" tabIndex={-1}>` is the fallback focus target.
 * - Render the edit bar, the gallery and the cells' edit chrome only while `active`.
 * - Focus requests are by element id (`editorCopy.ts`). They apply in a layout effect after
 *   the commit that renders them, except "after modal" ones (gallery close, reset result),
 *   applied in a passive effect so they land AFTER `<Modal>`'s passive-cleanup focus restore.
 * - Every pointer-drag announcement lives here too, including the 500 ms trailing debounce
 *   of the live-reorder position (`dragReorder`), so the drag layer announces nothing. The
 *   width radios use the same debounce, and consecutive widths in one Move-menu visit are ONE
 *   Undo step (arrowing through the radios checks each in turn).
 * - A keyboard or menu move focuses the Move button and scrolls its cell into view (it can
 *   land off-screen). Call `useLayoutEditor` BEFORE `useFlipLayout` in the page so this focus
 *   and scroll measure the committed layout before FLIP translates the cells back. The
 *   position is announced only when no focus event reached the button during the move: a
 *   re-focused button is read with its description, which already ends with the position.
 * - A save or reset that settles after the page unmounted (the user navigated away) still
 *   reports its outcome as a toast; the unsaved-changes guard is off while Done's save runs.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useUnsavedChanges } from '../../../hooks/useUnsavedChanges';
import type { DashboardLayoutResponse, DashboardLayoutValues } from '../../../types/dashboardLayout';
import { apiErrorDetail, apiErrorStatus } from '../../../utils/apiError';
import {
  addWidget,
  buildSavePayload,
  countCarriedWidgets,
  countUnknownWidgets,
  draftFromLayout,
  isSameLayout,
  moveWidgetTo,
  removeWidget,
  resolveLayout,
  setBannerVisible as withBannerVisible,
  setPanelSize as withPanelSize,
  zoneIds,
  type EditableZone,
  type LayoutDraft,
} from '../dashboardLayout';
import { DashboardLayoutCompanyChangedError, type UseDashboardLayoutResult } from '../useDashboardLayout';
import { DASHBOARD_REGISTRY, type WidgetRegistry } from '../widgetRegistry';
import {
  PANEL_SIZE_LABELS,
  type DashboardSourceKey,
  type DashboardWidgetDefinition,
  type PanelSize,
  type ResolvedLayout,
  type WidgetEligibilityContext,
} from '../widgetTypes';
import type { DragWidgetDescription } from './dragLayerTypes';
import { POSITION_ANNOUNCE_DEBOUNCE_MS } from './dragPlanning';
import * as copy from './editorCopy';
import { applySession, editorReducer, initialEditorCore, movedLabel, undoLabel, type EditorCore } from './editorState';

/** How long the last widget added from the gallery keeps its highlight ring. */
export const ADDED_HIGHLIGHT_MS = 1200;

export type EditorPending = 'save' | 'reset' | 'reload' | null;
export type ToastKind = 'success' | 'error' | 'warning' | 'info';

export interface UseLayoutEditorOptions {
  /** The session scope (`useDashboardSessionScope()`); the edit session is tagged with it. */
  scope: string | null;
  /** The page's `useDashboardLayout(scope)` result: saved layout, canEdit and the server verbs. */
  layout: UseDashboardLayoutResult;
  eligibility: WidgetEligibilityContext;
  registry?: WidgetRegistry;
  /** Called right before every committed layout change (pass `useFlipLayout(...).capture`). */
  onBeforeChange?: () => void;
  showToast: (type: ToastKind, message: string) => void;
  /** Write a message into the page's polite region (re-key it per message). */
  announce: (message: string) => void;
  /** prefers-reduced-motion: scroll-into-view jumps instead of gliding. */
  reducedMotion?: boolean;
}

export interface EditorDragging {
  zone: EditableZone;
  id: string;
}

export interface LayoutEditorState {
  /** True while an edit session exists FOR THE CURRENT SCOPE (false in the render a scope changes). */
  active: boolean;
  /** This edit session's id (the generation it was entered at); null when inactive. */
  sessionId: number | null;
  /** The committed draft (eligible widgets only); null when inactive. */
  draft: LayoutDraft | null;
  /**
   * What edit mode renders: the draft with any live drag order applied, run through
   * `buildSavePayload` + `resolveLayout` — exactly what Done would store, resolved as view
   * mode would resolve it. Null when inactive (the page then renders its own `resolved`).
   */
  shown: ResolvedLayout | null;
  /** Each editable zone's order as rendered (the drag layer's `orders`). Empty when inactive. */
  orders: { kpi: readonly string[]; panel: readonly string[] };
  /**
   * Changes whenever what edit mode renders changes geometry: banners shown, either zone's
   * order, a panel's width (the drag layer's `layoutKey`). '' when inactive.
   */
  layoutKey: string;
  /** The sources the committed draft needs (the page's debounced fetch union, docs §7). */
  draftNeededSources: readonly DashboardSourceKey[];
  /** The draft differs from the saved layout (an untouched or round-tripped draft is clean). */
  isDirty: boolean;
  pending: EditorPending;
  /** The bar's `role="alert"` text; '' when none. */
  error: string;
  /** A stale write (409 / company changed): only "Reload saved layout" (or Cancel) can proceed. */
  needsRefresh: boolean;
  /** The bar's status notice (the reload's "edits discarded" message); '' when none. */
  notice: string;
  /** Locks drag, the cells' chrome, Undo, Reset, Add widgets and Done: a request is in flight or a reload is required. */
  controlsDisabled: boolean;
  canUndo: boolean;
  /** The saved layout is not the stock default (Reset is meaningful). */
  hasSavedLayout: boolean;
  /** Saved widgets the viewer cannot see right now (kept on save). */
  carriedCount: number;
  /** Saved widgets this bundle does not know (a newer app saved them): dropped by a save from here. */
  unknownCount: number;
  dragging: EditorDragging | null;
  /** The widget whose Move menu is open (one at a time). */
  menuFor: string | null;
  galleryOpen: boolean;
  /** The gallery section to focus on open (a zone placeholder's "Add key metrics" / "Add panels"); null = the top. */
  galleryFocusZone: EditableZone | null;
  confirmResetOpen: boolean;
  /** The widget just added from the gallery, ringed for ADDED_HIGHLIGHT_MS after the gallery closes. */
  highlightId: string | null;
}

export type MovePlace = 'start' | 'end' | number;

export interface LayoutEditorVerbs {
  /**
   * Start editing, seeded ONCE from the saved layout. Pass the response of an error-path
   * re-read as `seed` when this render's `layout` may not show it yet. A read-only session
   * gets the info toast and is refused; a layout that is not ready is refused silently.
   * Returns whether edit mode is (now) active.
   */
  enter(seed?: DashboardLayoutResponse): boolean;
  /** One place earlier (-1) or later (+1) in reading order; announces the edge when it cannot. */
  moveBy(id: string, delta: -1 | 1): void;
  /** To the first / last place or a 0-based index (clamped). */
  moveTo(id: string, place: MovePlace): void;
  remove(id: string): void;
  /**
   * Append a KPI or panel to the end of its zone. `focusIds` is where focus goes next (the
   * gallery's next Add button, else the previous one, else its Done). Returns the
   * announcement for the GALLERY's live region, or null when nothing was added.
   */
  add(definition: DashboardWidgetDefinition, focusIds?: readonly string[]): string | null;
  setPanelSize(id: string, size: PanelSize): void;
  setBannerVisible(id: string, visible: boolean): void;
  undo(): void;
  dragStart(zone: EditableZone, id: string): void;
  dragReorder(zone: EditableZone, next: readonly string[]): void;
  dragEnd(zone: EditableZone, id: string): void;
  dragCancel(zone: EditableZone, id: string): void;
  openMenu(id: string): void;
  /** Close the open Move menu; `refocus` sends focus back to its Move button (Escape). */
  closeMenu(options?: { refocus?: boolean }): void;
  /** Open the gallery; `zone` (an empty zone's placeholder) lands on that section. */
  openGallery(zone?: EditableZone): void;
  /** Close the gallery; when something was added, focus its Move button after the Modal's restore. */
  closeGallery(): void;
  /** Done: leave when clean; otherwise one non-optimistic save. */
  done(): Promise<void>;
  /** Cancel: asks first when dirty (`confirmDiscard`), then leaves without sending anything. */
  requestCancel(): void;
  /** Open the Reset confirmation. */
  requestReset(): void;
  cancelReset(): void;
  /** The confirmed reset. */
  reset(): Promise<void>;
  /** "Reload saved layout": replace the draft with a fresh read (after a 409). */
  reload(): Promise<void>;
  /** The drag layer's `describe`: title and width label. */
  describe(id: string): DragWidgetDescription;
}

export type LayoutEditor = LayoutEditorState & LayoutEditorVerbs;

interface EditSession {
  scope: string | null;
  sessionId: number;
  core: EditorCore;
  pending: EditorPending;
  error: string;
  needsRefresh: boolean;
  notice: string;
  menuFor: string | null;
  /** Bumped on every Move-menu open: width changes coalesce into one Undo step per visit. */
  menuSeq: number;
  galleryOpen: boolean;
  galleryFocusZone: EditableZone | null;
  confirmReset: boolean;
  /** The last widget added since the gallery opened. */
  lastAdded: string | null;
}

interface FocusRequest {
  /** Tried in order: the first that exists and is enabled gets focus. */
  ids: readonly string[];
  afterModal: boolean;
  preventScroll: boolean;
  /** Scroll the target's cell into view (`block: 'nearest'`). */
  reveal: boolean;
  /**
   * Announced after focus is applied, unless a focus event reached the target meanwhile (the
   * screen reader then reads it with its description). Paired with `refocusWatchRef`.
   */
  announce?: string;
}

const CUSTOMIZE_TARGETS: readonly string[] = [copy.EDIT_IDS.customize, copy.EDIT_IDS.mainContent];
const NO_ORDERS: LayoutEditorState['orders'] = { kpi: [], panel: [] };
const NO_SOURCES: readonly DashboardSourceKey[] = [];

function isLocked(session: EditSession): boolean {
  return session.pending !== null || session.needsRefresh;
}

function applyFocus(request: FocusRequest, reducedMotion: boolean): void {
  for (const id of request.ids) {
    const element = document.getElementById(id);
    if (!element || element.matches(':disabled')) continue;
    element.focus(request.preventScroll || request.reveal ? { preventScroll: true } : undefined);
    if (request.reveal) {
      const target = element.closest('[data-widget-cell]') ?? element;
      // A glide still running from an earlier change would skew the measurement: settle it,
      // so the scroll targets where the cell IS (the page plays this commit's FLIP afterwards).
      if (typeof target.getAnimations === 'function') target.getAnimations().forEach(animation => animation.cancel());
      if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'nearest', behavior: reducedMotion ? 'auto' : 'smooth' });
      }
    }
    return;
  }
}

export function useLayoutEditor(options: UseLayoutEditorOptions): LayoutEditor {
  const { scope, layout, eligibility } = options;
  const registry = options.registry ?? DASHBOARD_REGISTRY;
  const [session, setSession] = useState<EditSession | null>(null);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const [highlightId, setHighlightId] = useState<string | null>(null);

  // Verbs read the LATEST render's options and the latest written session through refs, so
  // they are stable and safe to call after an await.
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const sessionRef = useRef(session);
  const generationRef = useRef(0);
  const mountedRef = useRef(true);
  const announceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Whether the session was dirty at its last active render (for the scope-change discard toast). */
  const lastDirtyRef = useRef(false);
  /** A move's Move button, and whether a focus event reached it during the move's commit. */
  const refocusWatchRef = useRef<{ id: string; seen: boolean } | null>(null);

  const active = session !== null && session.scope === scope;
  const current = active ? session : null;
  const saved = layout.saved;

  const committedDraft = current?.core.draft ?? null;
  const dragSession = current?.core.session ?? null;
  const renderDraft = useMemo(
    () => (committedDraft ? applySession(committedDraft, dragSession) : null),
    [committedDraft, dragSession]
  );
  const shown = useMemo(
    () =>
      renderDraft
        ? resolveLayout(buildSavePayload(renderDraft, saved, eligibility, registry), eligibility, registry)
        : null,
    [renderDraft, saved, eligibility, registry]
  );
  const payload = useMemo(
    () => (committedDraft ? buildSavePayload(committedDraft, saved, eligibility, registry) : null),
    [committedDraft, saved, eligibility, registry]
  );
  const isDirty = payload !== null && !isSameLayout(payload, saved, registry);
  if (active) lastDirtyRef.current = isDirty;
  const carriedCount = useMemo(
    () => (active ? countCarriedWidgets(saved, eligibility, registry) : 0),
    [active, saved, eligibility, registry]
  );
  const unknownCount = useMemo(() => (active ? countUnknownWidgets(saved, registry) : 0), [active, saved, registry]);
  const orders = useMemo(
    () =>
      shown
        ? { kpi: shown.kpis.map(definition => definition.id), panel: shown.panels.map(panel => panel.definition.id) }
        : NO_ORDERS,
    [shown]
  );

  const layoutKey = useMemo(
    () =>
      shown
        ? JSON.stringify([
            shown.banners.map(definition => definition.id),
            shown.kpis.map(definition => definition.id),
            shown.panels.map(panel => [panel.definition.id, panel.size]),
          ])
        : '',
    [shown]
  );

  // Off while Done's save runs: leaving then does not discard anything (the PUT still lands, and
  // its outcome is toasted even after the page unmounts), so "leave and discard" would be false.
  const unsaved = useUnsavedChanges(active && isDirty && current?.pending !== 'save');
  const unsavedRef = useRef(unsaved);
  unsavedRef.current = unsaved;

  /** End the edit session (no announcement, no focus) and bump the generation, so a late result is ignored. */
  const endSessionRef = useRef(() => {
    generationRef.current += 1;
    if (announceTimerRef.current !== null) clearTimeout(announceTimerRef.current);
    announceTimerRef.current = null;
    if (highlightTimerRef.current !== null) clearTimeout(highlightTimerRef.current);
    highlightTimerRef.current = null;
    setHighlightId(null);
    if (sessionRef.current !== null) {
      sessionRef.current = null;
      setSession(null);
    }
  });

  const verbs = useMemo<LayoutEditorVerbs>(() => {
    const registryOf = () => optionsRef.current.registry ?? DASHBOARD_REGISTRY;
    const titleOf = (id: string) => registryOf().byId.get(id)?.title ?? id;
    const zoneOf = (id: string): EditableZone | null => {
      const zone = registryOf().byId.get(id)?.zone;
      return zone === 'kpi' || zone === 'panel' ? zone : null;
    };
    const activeSession = (): EditSession | null => {
      const value = sessionRef.current;
      return value && value.scope === optionsRef.current.scope ? value : null;
    };
    const write = (next: EditSession | null) => {
      sessionRef.current = next;
      setSession(next);
    };
    const isStale = (generation: number) => !mountedRef.current || generation !== generationRef.current;
    const requestFocus = (ids: readonly string[], extra: Partial<Omit<FocusRequest, 'ids'>> = {}) => {
      setFocusRequest({ ids, afterModal: false, preventScroll: false, reveal: false, ...extra });
    };
    const clearAnnounceTimer = () => {
      if (announceTimerRef.current !== null) clearTimeout(announceTimerRef.current);
      announceTimerRef.current = null;
    };
    /** Announce `message` once nothing has changed for POSITION_ANNOUNCE_DEBOUNCE_MS (trailing). */
    const announceSettled = (message: string) => {
      const generation = generationRef.current;
      clearAnnounceTimer();
      announceTimerRef.current = setTimeout(() => {
        announceTimerRef.current = null;
        if (!isStale(generation)) optionsRef.current.announce(message);
      }, POSITION_ANNOUNCE_DEBOUNCE_MS);
    };
    const payloadOf = (value: EditSession): DashboardLayoutValues => {
      const { layout: currentLayout, eligibility: currentEligibility } = optionsRef.current;
      return buildSavePayload(value.core.draft, currentLayout.saved, currentEligibility, registryOf());
    };
    const isDirtyNow = (value: EditSession) =>
      !isSameLayout(payloadOf(value), optionsRef.current.layout.saved, registryOf());
    const beforeChange = () => optionsRef.current.onBeforeChange?.();

    const endSession = () => endSessionRef.current();
    /** Leave edit mode, focus the header Customize button, optionally announcing. */
    const finish = (announcement?: string, afterModal = false) => {
      endSession();
      if (announcement) optionsRef.current.announce(announcement);
      requestFocus(CUSTOMIZE_TARGETS, { afterModal });
    };
    /** A failed save or reset: a stale write locks the editor behind "Reload saved layout". */
    const describeFailure = (failure: unknown, fallback: string, focusId: string, afterModal: boolean) => {
      const value = activeSession();
      if (!value) return;
      const conflict = apiErrorStatus(failure) === 409 || failure instanceof DashboardLayoutCompanyChangedError;
      write({
        ...value,
        pending: null,
        confirmReset: false,
        needsRefresh: value.needsRefresh || conflict,
        error: apiErrorDetail(failure, fallback),
      });
      requestFocus([conflict ? copy.EDIT_IDS.reload : focusId], { afterModal });
    };

    const moveInternal = (id: string, place: MovePlace, earlier: boolean) => {
      const value = activeSession();
      if (!value || isLocked(value) || value.core.session) return;
      const zone = zoneOf(id);
      if (!zone) return;
      const order = zoneIds(value.core.draft, zone);
      const from = order.indexOf(id);
      if (from < 0) return;
      const last = order.length - 1;
      const to = place === 'start' ? 0 : place === 'end' ? last : Math.max(0, Math.min(last, Math.trunc(place)));
      const title = titleOf(id);
      const { announce } = optionsRef.current;
      if (to === from) {
        if (value.menuFor !== null) write({ ...value, menuFor: null });
        if (earlier && from === 0) announce(copy.alreadyFirstMessage(title));
        else if (!earlier && from === last) announce(copy.alreadyLastMessage(title));
        return;
      }
      const core = editorReducer(value.core, {
        type: 'apply',
        next: draft => moveWidgetTo(draft, zone, id, to),
        label: movedLabel(title),
      });
      if (core === value.core) return;
      beforeChange();
      write({ ...value, core, menuFor: null });
      // React's keyed reorder re-inserts a cell moved LATER (focus drops and React restores it),
      // and a menu move focuses the button afresh: either focus event makes the screen reader
      // read the button's description, which ends with the new position. Announce only when no
      // focus event reached it (a move earlier, which leaves the focused node in place).
      const buttonId = copy.moveButtonId(id);
      refocusWatchRef.current = { id: buttonId, seen: false };
      requestFocus([buttonId], { reveal: true, announce: copy.positionMessage(title, to + 1, order.length) });
    };

    return {
      enter(seed) {
        if (activeSession()) return true;
        const { layout: currentLayout, eligibility: currentEligibility, showToast, announce } = optionsRef.current;
        if (!seed && currentLayout.status !== 'ready') return false;
        const canEdit = seed ? seed.can_edit === true : currentLayout.canEdit;
        if (!canEdit) {
          showToast('info', copy.READ_ONLY_LAYOUT_NOTICE);
          return false;
        }
        const seedLayout = seed ? seed.layout : currentLayout.saved;
        generationRef.current += 1;
        lastDirtyRef.current = false;
        write({
          scope: optionsRef.current.scope,
          sessionId: generationRef.current,
          core: initialEditorCore(draftFromLayout(seedLayout, currentEligibility, registryOf())),
          pending: null,
          error: '',
          needsRefresh: false,
          notice: '',
          menuFor: null,
          menuSeq: 0,
          galleryOpen: false,
          galleryFocusZone: null,
          confirmReset: false,
          lastAdded: null,
        });
        announce(copy.ENTER_ANNOUNCEMENT);
        requestFocus([copy.EDIT_IDS.heading]);
        return true;
      },

      moveBy(id, delta) {
        const value = activeSession();
        const zone = zoneOf(id);
        if (!value || !zone) return;
        const from = zoneIds(value.core.draft, zone).indexOf(id);
        if (from < 0) return;
        moveInternal(id, from + delta, delta < 0);
      },

      moveTo(id, place) {
        const value = activeSession();
        const zone = zoneOf(id);
        if (!value || !zone) return;
        const from = zoneIds(value.core.draft, zone).indexOf(id);
        moveInternal(id, place, place === 'start' || (typeof place === 'number' && place < from));
      },

      remove(id) {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session) return;
        const zone = zoneOf(id);
        if (!zone) return;
        const order = zoneIds(value.core.draft, zone);
        const index = order.indexOf(id);
        if (index < 0) return;
        const title = titleOf(id);
        const core = editorReducer(value.core, {
          type: 'apply',
          next: draft => removeWidget(draft, zone, id),
          label: copy.removedLabel(title),
        });
        if (core === value.core) return;
        beforeChange();
        write({ ...value, core, menuFor: value.menuFor === id ? null : value.menuFor });
        optionsRef.current.announce(copy.removedMessage(title));
        const neighbour = order[index + 1] ?? order[index - 1];
        requestFocus(neighbour ? [copy.removeButtonId(neighbour)] : [copy.EDIT_IDS.add]);
      },

      add(definition, focusIds) {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session) return null;
        if (definition.zone !== 'kpi' && definition.zone !== 'panel') return null;
        const zone: EditableZone = definition.zone;
        const core = editorReducer(value.core, {
          type: 'apply',
          next: draft => addWidget(draft, definition),
          label: copy.addedLabel(definition.title),
        });
        if (core === value.core) return null;
        beforeChange();
        write({ ...value, core, lastAdded: definition.id });
        if (focusIds && focusIds.length > 0) requestFocus(focusIds);
        return copy.addedMessage(definition.title, zoneIds(core.draft, zone).length);
      },

      setPanelSize(id, size) {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session) return;
        const title = titleOf(id);
        const core = editorReducer(value.core, {
          type: 'apply',
          next: draft => withPanelSize(draft, id, size),
          label: copy.resizedLabel(title, size),
          // Arrow keys check each radio in turn: one Undo step per panel per menu visit.
          coalesceKey: `resize:${id}:${value.menuSeq}`,
        });
        if (core === value.core) return;
        beforeChange();
        write({ ...value, core });
        announceSettled(copy.widthMessage(title, size));
      },

      setBannerVisible(id, visible) {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session) return;
        const core = editorReducer(value.core, {
          type: 'apply',
          next: draft => withBannerVisible(draft, id, visible),
          label: copy.bannerLabel(titleOf(id), visible),
        });
        if (core === value.core) return;
        beforeChange();
        write({ ...value, core });
      },

      undo() {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session) return;
        const label = undoLabel(value.core);
        if (label === null) return;
        beforeChange();
        write({ ...value, core: editorReducer(value.core, { type: 'undo' }), menuFor: null });
        optionsRef.current.announce(copy.undoneMessage(label));
      },

      dragStart(zone, id) {
        const value = activeSession();
        if (!value || isLocked(value)) return;
        const core = editorReducer(value.core, { type: 'sessionStart', zone, activeId: id });
        if (core === value.core || !core.session) return;
        clearAnnounceTimer();
        write({ ...value, core, menuFor: null });
        const order = core.session.startOrder;
        optionsRef.current.announce(copy.pickedUpMessage(titleOf(id), order.indexOf(id) + 1, order.length));
      },

      dragReorder(zone, next) {
        const value = activeSession();
        if (!value || !value.core.session) return;
        const core = editorReducer(value.core, { type: 'sessionReorder', zone, order: next });
        if (core === value.core || !core.session) return;
        beforeChange();
        write({ ...value, core });
        const { activeId, order } = core.session;
        announceSettled(copy.positionMessage(titleOf(activeId), order.indexOf(activeId) + 1, order.length));
      },

      dragEnd() {
        clearAnnounceTimer();
        const value = activeSession();
        const drag = value?.core.session;
        if (!value || !drag) return;
        const title = titleOf(drag.activeId);
        const from = drag.startOrder.indexOf(drag.activeId);
        const to = drag.order.indexOf(drag.activeId);
        write({ ...value, core: editorReducer(value.core, { type: 'sessionEnd', title }) });
        optionsRef.current.announce(
          to === from
            ? copy.staysMessage(title, from + 1, drag.order.length)
            : copy.droppedMessage(title, to + 1, drag.order.length)
        );
        requestFocus([copy.moveButtonId(drag.activeId)], { preventScroll: true });
      },

      dragCancel() {
        clearAnnounceTimer();
        const value = activeSession();
        const drag = value?.core.session;
        if (!value || !drag) return;
        const moved = drag.order.some((id, index) => id !== drag.startOrder[index]);
        if (moved) beforeChange();
        write({ ...value, core: editorReducer(value.core, { type: 'sessionCancel' }) });
        const from = drag.startOrder.indexOf(drag.activeId);
        optionsRef.current.announce(
          copy.moveCancelledMessage(titleOf(drag.activeId), from + 1, drag.startOrder.length)
        );
        requestFocus([copy.moveButtonId(drag.activeId)], { preventScroll: true });
      },

      openMenu(id) {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session || value.menuFor === id) return;
        write({ ...value, menuFor: id, menuSeq: value.menuSeq + 1 });
      },

      closeMenu(closeOptions) {
        const value = activeSession();
        if (!value || value.menuFor === null) return;
        const id = value.menuFor;
        write({ ...value, menuFor: null });
        if (closeOptions?.refocus) requestFocus([copy.moveButtonId(id)]);
      },

      openGallery(zone) {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session || value.galleryOpen) return;
        write({ ...value, galleryOpen: true, galleryFocusZone: zone ?? null, lastAdded: null, menuFor: null });
      },

      closeGallery() {
        const value = activeSession();
        if (!value || !value.galleryOpen) return;
        const added = value.lastAdded;
        write({ ...value, galleryOpen: false, galleryFocusZone: null, lastAdded: null });
        if (!added) return;
        requestFocus([copy.moveButtonId(added)], { afterModal: true, reveal: true });
        if (highlightTimerRef.current !== null) clearTimeout(highlightTimerRef.current);
        setHighlightId(added);
        highlightTimerRef.current = setTimeout(() => {
          highlightTimerRef.current = null;
          setHighlightId(null);
        }, ADDED_HIGHLIGHT_MS);
      },

      async done() {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session) return;
        const { layout: currentLayout, showToast } = optionsRef.current;
        if (!currentLayout.canEdit) {
          showToast('info', copy.READ_ONLY_LAYOUT_NOTICE);
          return;
        }
        const body = payloadOf(value);
        if (isSameLayout(body, currentLayout.saved, registryOf())) {
          finish(copy.DONE_UNCHANGED_ANNOUNCEMENT);
          return;
        }
        const generation = generationRef.current;
        // The toast provider sits above the router: it still shows after the page unmounted.
        const toastAfterLeaving = showToast;
        write({ ...value, pending: 'save', error: '', notice: '', menuFor: null });
        try {
          await currentLayout.save(body);
          // The user navigated away while the PUT ran: it still landed, so say so.
          if (!mountedRef.current) {
            toastAfterLeaving('success', copy.SAVED_TOAST);
            return;
          }
          if (isStale(generation)) return;
          unsavedRef.current.markSaved();
          optionsRef.current.showToast('success', copy.SAVED_TOAST);
          finish();
        } catch (failure) {
          if (!mountedRef.current) {
            toastAfterLeaving('error', copy.SAVE_FAILED_AFTER_LEAVING);
            return;
          }
          if (isStale(generation)) return;
          describeFailure(failure, copy.SAVE_FAILED, copy.EDIT_IDS.done, false);
        }
      },

      requestCancel() {
        const value = activeSession();
        if (!value || value.pending !== null) return;
        const dirty = isDirtyNow(value);
        if (dirty && !unsavedRef.current.confirmDiscard()) return;
        if (dirty || value.core.session) beforeChange();
        const stale = value.needsRefresh;
        finish(copy.CANCELLED_ANNOUNCEMENT);
        // A stale write (409) means this tab's copy is behind the server: catch up, so the view
        // and the next edit session start from the saved layout and its current version.
        // `layout.reload` never rejects, and a failed re-read never downgrades a ready layout.
        if (stale) void optionsRef.current.layout.reload();
      },

      requestReset() {
        const value = activeSession();
        if (!value || isLocked(value) || value.core.session || optionsRef.current.layout.saved === null) return;
        write({ ...value, confirmReset: true, menuFor: null });
      },

      cancelReset() {
        const value = activeSession();
        if (!value || !value.confirmReset || value.pending === 'reset') return;
        write({ ...value, confirmReset: false });
      },

      async reset() {
        const value = activeSession();
        if (!value || isLocked(value)) return;
        const { layout: currentLayout, showToast } = optionsRef.current;
        if (currentLayout.saved === null) return;
        if (!currentLayout.canEdit) {
          showToast('info', copy.READ_ONLY_LAYOUT_NOTICE);
          return;
        }
        const generation = generationRef.current;
        const toastAfterLeaving = showToast;
        write({ ...value, pending: 'reset', error: '', notice: '', menuFor: null });
        try {
          await currentLayout.reset();
          if (!mountedRef.current) {
            toastAfterLeaving('success', copy.RESET_TOAST);
            return;
          }
          if (isStale(generation)) return;
          unsavedRef.current.markSaved();
          beforeChange();
          optionsRef.current.showToast('success', copy.RESET_TOAST);
          // The confirmation's Modal restores focus in a passive cleanup; land after it.
          finish(undefined, true);
        } catch (failure) {
          if (!mountedRef.current) {
            toastAfterLeaving('error', copy.RESET_FAILED_AFTER_LEAVING);
            return;
          }
          if (isStale(generation)) return;
          describeFailure(failure, copy.RESET_FAILED, copy.EDIT_IDS.reset, true);
        }
      },

      async reload() {
        const value = activeSession();
        if (!value || value.pending !== null) return;
        const generation = generationRef.current;
        write({ ...value, pending: 'reload', notice: '' });
        try {
          // `useDashboardLayout.reload` resolves null on failure (it never rejects for HTTP).
          const response = await optionsRef.current.layout.reload();
          if (isStale(generation)) return;
          if (!response) throw new Error(copy.RELOAD_FAILED);
          const latest = activeSession();
          if (!latest) return;
          beforeChange();
          write({
            ...latest,
            core: editorReducer(latest.core, {
              type: 'replace',
              draft: draftFromLayout(response.layout, optionsRef.current.eligibility, registryOf()),
            }),
            pending: null,
            error: '',
            needsRefresh: false,
            notice: copy.RELOADED_NOTICE,
            menuFor: null,
          });
          requestFocus([copy.EDIT_IDS.heading]);
        } catch (failure) {
          if (isStale(generation)) return;
          const latest = activeSession();
          if (!latest) return;
          write({ ...latest, pending: null, error: apiErrorDetail(failure, copy.RELOAD_FAILED) });
          requestFocus([copy.EDIT_IDS.reload]);
        }
      },

      describe(id) {
        const value = activeSession();
        const definition = registryOf().byId.get(id);
        const title = definition?.title ?? id;
        if (definition?.zone !== 'panel') return { title, sizeLabel: copy.KPI_SIZE_LABEL };
        const draft = value ? applySession(value.core.draft, value.core.session) : null;
        const size = draft?.panels.find(panel => panel.id === id)?.size ?? definition.defaultSize ?? 'half';
        return { title, sizeLabel: PANEL_SIZE_LABELS[size] };
      },
    };
  }, []);

  // A session from another scope is already inactive (same render); clear it here.
  useEffect(() => {
    if (session === null || session.scope === scope) return;
    const wasDirty = lastDirtyRef.current;
    endSessionRef.current();
    if (wasDirty) optionsRef.current.showToast('warning', copy.SESSION_CHANGED_TOAST);
    // The bar and the chrome unmounted in the render that made the session inactive, so
    // focus that was inside the editor has dropped to <body>.
    const focused = document.activeElement;
    if (!focused || focused === document.body) {
      applyFocus(
        { ids: CUSTOMIZE_TARGETS, afterModal: false, preventScroll: false, reveal: false },
        optionsRef.current.reducedMotion === true
      );
    }
  }, [session, scope]);

  // The session became read-only (a reload answered can_edit: false): leave edit mode.
  useEffect(() => {
    if (!active || layout.status !== 'ready' || layout.canEdit) return;
    endSessionRef.current();
    optionsRef.current.showToast('info', copy.READ_ONLY_LAYOUT_NOTICE);
    setFocusRequest({ ids: CUSTOMIZE_TARGETS, afterModal: false, preventScroll: false, reveal: false });
  }, [active, layout.status, layout.canEdit]);

  // Every focus event: did one reach the button a move is watching (React restoring focus after
  // a keyed re-insert, or the move's own focus call)?
  useEffect(() => {
    const onFocusIn = (event: FocusEvent) => {
      const watch = refocusWatchRef.current;
      if (watch && event.target instanceof Element && event.target.id === watch.id) watch.seen = true;
    };
    document.addEventListener('focusin', onFocusIn, true);
    return () => document.removeEventListener('focusin', onFocusIn, true);
  }, []);

  useLayoutEffect(() => {
    if (!focusRequest || focusRequest.afterModal) return;
    applyFocus(focusRequest, optionsRef.current.reducedMotion === true);
    if (focusRequest.announce === undefined) return;
    const watch = refocusWatchRef.current;
    refocusWatchRef.current = null;
    if (!watch?.seen) optionsRef.current.announce(focusRequest.announce);
  }, [focusRequest]);
  useEffect(() => {
    if (focusRequest && focusRequest.afterModal) applyFocus(focusRequest, optionsRef.current.reducedMotion === true);
  }, [focusRequest]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      if (announceTimerRef.current !== null) clearTimeout(announceTimerRef.current);
      if (highlightTimerRef.current !== null) clearTimeout(highlightTimerRef.current);
    };
  }, []);

  const controlsDisabled = current !== null && isLocked(current);
  return {
    ...verbs,
    active,
    sessionId: current?.sessionId ?? null,
    draft: committedDraft,
    shown,
    orders,
    layoutKey,
    draftNeededSources: shown?.neededSources ?? NO_SOURCES,
    isDirty,
    pending: current?.pending ?? null,
    error: current?.error ?? '',
    needsRefresh: current?.needsRefresh ?? false,
    notice: current?.notice ?? '',
    controlsDisabled,
    canUndo: current !== null && current.core.past.length > 0,
    hasSavedLayout: saved !== null,
    carriedCount,
    unknownCount,
    dragging: dragSession ? { zone: dragSession.zone, id: dragSession.activeId } : null,
    menuFor: current?.menuFor ?? null,
    galleryOpen: current?.galleryOpen ?? false,
    galleryFocusZone: current?.galleryFocusZone ?? null,
    confirmResetOpen: current?.confirmReset ?? false,
    highlightId: active ? highlightId : null,
  };
}
