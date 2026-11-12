/**
 * Dashboard edit mode: the copy and element ids more than one module must agree on — every
 * message `useLayoutEditor` announces or toasts, the Move / Remove names and zone labels, and
 * every element id the hook moves focus to.
 *
 * One module so the hook, the bar, the cells, the menu and the gallery cannot disagree about
 * a message or an id, and so tests import the copy instead of re-typing it. Text only one
 * component renders (the bar's heading, the Move menu's items, the gallery's own text) stays
 * in that component.
 *
 * Focus is requested BY ELEMENT ID (`useLayoutEditor`): React's keyed reorder re-inserts a
 * moved cell's DOM nodes, which blurs whatever was focused inside it, so a ref captured
 * before the move is not enough — the control is looked up again after the commit.
 */
import { PANEL_SIZE_LABELS, type PanelSize } from '../widgetTypes';

/** The read-only refusal (toast on Customize; also the exit toast when `canEdit` turns false). */
export const READ_ONLY_LAYOUT_NOTICE =
  "This session can't save a dashboard layout — read-only company view or an API/kiosk credential.";

/** sr-only description on the header Customize button for a read-only session. */
export const READ_ONLY_CUSTOMIZE_DESCRIPTION = "Read-only session. The layout can't be changed.";

/** Error toast when the lazily loaded drag layer fails to import (a chunk 404 after a deploy). */
export const EDITOR_LOAD_FAILED = "The dashboard editor couldn't load. Reload the page and try again.";

// ── Element ids ────────────────────────────────────────────────────────────────

/**
 * Widget ids match `^[a-z][a-z0-9_.]{0,63}$` and can never contain `-`, so replacing every
 * character outside `[a-zA-Z0-9_-]` with `-` is injective: two widgets never share an id.
 */
export const safeId = (widgetId: string) => widgetId.replace(/[^a-zA-Z0-9_-]/g, '-');

export const EDIT_IDS = {
  /** The page's header Customize button. The page MUST give it this id. */
  customize: 'dashboard-customize',
  /** The app layout's `<main>` (tabIndex -1): the fallback when Customize is disabled. */
  mainContent: 'main-content',
  bar: 'dashboard-edit-bar',
  heading: 'dashboard-edit-heading',
  reload: 'dashboard-edit-reload',
  add: 'dashboard-edit-add',
  undo: 'dashboard-edit-undo',
  reset: 'dashboard-edit-reset',
  cancel: 'dashboard-edit-cancel',
  done: 'dashboard-edit-done',
  /** The shared, `hidden` keyboard help every Move button is described by (rendered in the bar). */
  moveHelp: 'dashboard-edit-move-help',
  galleryHeading: 'dashboard-gallery-heading',
  galleryDone: 'dashboard-gallery-done',
} as const;

export const moveButtonId = (widgetId: string) => `dashboard-edit-move-${safeId(widgetId)}`;
export const removeButtonId = (widgetId: string) => `dashboard-edit-remove-${safeId(widgetId)}`;
export const arrangeMenuId = (widgetId: string) => `dashboard-edit-menu-${safeId(widgetId)}`;
export const positionTextId = (widgetId: string) => `dashboard-edit-position-${safeId(widgetId)}`;
export const galleryAddId = (widgetId: string) => `dashboard-gallery-add-${safeId(widgetId)}`;

// ── Accessible names ─────────────────────────────────────────────────────────────

export const removeLabel = (title: string) => `Remove ${title}`;
/** The Move button's (and the Move menu group's) name: panels can also be resized. */
export const moveLabel = (title: string, zone: 'kpi' | 'panel') =>
  zone === 'panel' ? `Move or resize ${title}` : `Move ${title}`;
export const ZONE_LIST_LABELS = {
  kpi: 'Key metrics on your dashboard',
  panel: 'Panels on your dashboard',
} as const;

export const MOVE_HELP =
  'Use the arrow keys to move this widget one place. Home and End move it to the first or last place. ' +
  'Press Enter for more options.';

/** The empty-zone placeholders (edit mode only) and their gallery buttons. */
export const ZONE_PLACEHOLDERS = {
  kpi: { message: 'No key metrics on your dashboard.', action: 'Add key metrics' },
  panel: { message: 'No panels on your dashboard.', action: 'Add panels' },
} as const;

// ── Announcements (the page's polite region, except `addedMessage`: the gallery's own) ──

export const ENTER_ANNOUNCEMENT =
  "Customizing your dashboard. Drag widgets to rearrange them, or use each widget's Move button. Press Done to save.";
export const positionMessage = (title: string, position: number, count: number) =>
  `${title}, position ${position} of ${count}.`;
export const alreadyFirstMessage = (title: string) => `${title} is already first.`;
export const alreadyLastMessage = (title: string) => `${title} is already last.`;
export const widthMessage = (title: string, size: PanelSize) => `${title} width: ${PANEL_SIZE_LABELS[size]}.`;
export const removedMessage = (title: string) => `${title} removed from your dashboard. Undo is available.`;
export const addedMessage = (title: string, position: number) =>
  `${title} added at position ${position} of ${position}.`;
export const undoneMessage = (label: string) => `Undone: ${label}.`;
export const pickedUpMessage = (title: string, position: number, count: number) =>
  `Picked up ${title}, position ${position} of ${count}.`;
export const droppedMessage = (title: string, position: number, count: number) =>
  `${title} dropped at position ${position} of ${count}.`;
export const staysMessage = (title: string, position: number, count: number) =>
  `${title} stays at position ${position} of ${count}.`;
export const moveCancelledMessage = (title: string, position: number, count: number) =>
  `Move cancelled. ${title} is back at position ${position} of ${count}.`;
export const DONE_UNCHANGED_ANNOUNCEMENT = 'Finished customizing. Nothing changed.';
export const CANCELLED_ANNOUNCEMENT = 'Customizing cancelled. Your saved layout is showing.';
/** Written into the bar's always-mounted status region after a successful reload. */
export const RELOADED_NOTICE = 'Reloaded your saved layout. Your unsaved changes were discarded.';
/** The bar's status region while Done's save / the reload is in flight (the button keeps focus). */
export const SAVING_NOTICE = 'Saving your dashboard layout…';
export const RELOADING_NOTICE = 'Reloading your saved layout…';

// ── Undo history labels ("Undone: {label}.") ─────────────────────────────────────

export const removedLabel = (title: string) => `removed ${title}`;
export const addedLabel = (title: string) => `added ${title}`;
export const resizedLabel = (title: string, size: PanelSize) => `${title} width ${PANEL_SIZE_LABELS[size]}`;
export const bannerLabel = (title: string, visible: boolean) => `${visible ? 'showed' : 'hid'} ${title}`;

// ── Toasts and failures ──────────────────────────────────────────────────────────

export const SAVED_TOAST = 'Dashboard layout saved';
export const RESET_TOAST = 'Dashboard reset to default';
export const SESSION_CHANGED_TOAST = 'Your session changed, so unsaved dashboard edits were discarded.';
export const SAVE_FAILED = 'Your dashboard layout was not saved. Try again.';
export const RESET_FAILED = 'Your dashboard was not reset. Try again.';
/**
 * A save / reset that failed after the user navigated away: the draft is gone, so there is
 * nothing to "try again" — say what happened and where the layout stands.
 */
export const SAVE_FAILED_AFTER_LEAVING =
  'Your dashboard layout changes were not saved. Your previous layout is unchanged.';
export const RESET_FAILED_AFTER_LEAVING = 'Your dashboard was not reset. Your saved layout is unchanged.';
export const RELOAD_FAILED = 'Your saved layout could not be reloaded. Try again.';

/** The edit bar's note about saved widgets the viewer cannot see right now (they are kept on save). */
export function carriedNotice(count: number): string {
  return count === 1
    ? '1 widget in your saved layout is not available to you right now; it is kept and will reappear if access returns.'
    : `${count} widgets in your saved layout are not available to you right now; they are kept and will reappear if access returns.`;
}

/**
 * The edit bar's note about saved widgets THIS bundle does not know (a newer version of the
 * app saved them): a save from here drops them, so say so before Done.
 */
export function unknownWidgetsNotice(count: number): string {
  return count === 1
    ? "1 widget from a newer version of the app isn't shown here and will be removed if you save. Refresh the page to keep it."
    : `${count} widgets from a newer version of the app aren't shown here and will be removed if you save. Refresh the page to keep them.`;
}

/** The ghost card's and the layer's width label for a KPI tile (panels use PANEL_SIZE_LABELS). */
export const KPI_SIZE_LABEL = 'Key metric';
