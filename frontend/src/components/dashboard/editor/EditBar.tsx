/**
 * The dashboard edit bar: fixed to the bottom of the viewport while editing, holding
 * **Add widgets · Undo · Reset · Cancel · Done**, the failure alert with its recovery action,
 * the notice region, the carried-widget note and the shared keyboard help for Move buttons.
 *
 * Contract:
 * - Pass `editor` = `useLayoutEditor()`. The bar renders nothing while `editor.active` is
 *   false, so a scope change hides it in the same render the session goes inactive.
 * - Render it right after the page header in the DOM (it is fixed at the bottom visually, but
 *   its controls come next in the tab order). It sits above BottomNav below lg and right of
 *   the sidebar at lg+ (`z-[45]`: above BottomNav 40, below the sidebar 50 and Modal 60); the
 *   page adds `pb-28` so nothing is hidden under it. `m-0` beats the page root's `space-y-4`
 *   (Tailwind 4 gives every non-last child a `margin-block-end`, which would lift a fixed
 *   `bottom-0` bar 1rem off the viewport edge).
 * - Done is ONE non-optimistic save (a spinner while it runs; clean = leave with no request).
 *   While a request is in flight Add widgets, Undo, Reset and Cancel are disabled. After a
 *   stale write (409 / company changed) the `role="alert"` shows the server's detail with a
 *   focused **Reload saved layout**, which stays mounted (spinner) while the reload runs;
 *   Done and Reset stay locked until it succeeds. Cancel stays available (it sends nothing).
 *   The in-flight Done / Reload stays FOCUSABLE (`aria-disabled`, clicks ignored), so keyboard
 *   focus never drops to <body>, and the status region says "Saving…" / "Reloading…".
 * - Undo uses `aria-disabled`, never `disabled`, so focus on it never drops; with nothing to
 *   undo a click is a no-op.
 * - Reset opens a `ConfirmDialog` (pending while the reset runs); it is unavailable when the
 *   saved layout already is the stock default.
 * - The status region is always mounted, so the reload's "edits discarded" notice is spoken.
 * - The heading (entry and a successful reload focus it) is visible at every width.
 * - A saved layout naming widgets this bundle does not know gets a note: a save drops them.
 * - The hidden `#dashboard-edit-move-help` paragraph is what every Move button's
 *   `aria-describedby` points at.
 */
import React from 'react';
import { ArrowUturnLeftIcon, PlusIcon } from '@heroicons/react/24/outline';
import { Button, ConfirmDialog, LoadingButton } from '../../ui';
import {
  EDIT_IDS,
  MOVE_HELP,
  RELOADING_NOTICE,
  SAVING_NOTICE,
  carriedNotice,
  unknownWidgetsNotice,
} from './editorCopy';
import type { LayoutEditor } from './useLayoutEditor';

/** What the bar reads from `useLayoutEditor()`. */
export type EditBarEditor = Pick<
  LayoutEditor,
  | 'active'
  | 'isDirty'
  | 'pending'
  | 'error'
  | 'needsRefresh'
  | 'notice'
  | 'controlsDisabled'
  | 'canUndo'
  | 'hasSavedLayout'
  | 'carriedCount'
  | 'unknownCount'
  | 'confirmResetOpen'
  | 'openGallery'
  | 'undo'
  | 'requestReset'
  | 'cancelReset'
  | 'reset'
  | 'requestCancel'
  | 'done'
  | 'reload'
>;

export interface EditBarProps {
  editor: EditBarEditor;
}

export function EditBar({ editor }: EditBarProps) {
  if (!editor.active) return null;
  const { pending, controlsDisabled } = editor;
  const undoUnavailable = !editor.canUndo || controlsDisabled;
  const resetDisabled = controlsDisabled || !editor.hasSavedLayout;
  const pendingNotice = pending === 'save' ? SAVING_NOTICE : pending === 'reload' ? RELOADING_NOTICE : '';

  return (
    <section
      id={EDIT_IDS.bar}
      aria-labelledby={EDIT_IDS.heading}
      className="fixed inset-x-0 bottom-[calc(4rem+env(safe-area-inset-bottom,0px))] z-[45] m-0 border-t border-fd-line-bright bg-fd-panel/95 lg:bottom-0 lg:left-72"
    >
      <div className="flex flex-col gap-2 px-4 py-2 sm:px-6 lg:px-8">
        {editor.error && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-2 border border-red-500/40 bg-red-500/10 p-2 text-sm text-red-200"
          >
            <p className="min-w-0 flex-1">{editor.error}</p>
            {editor.needsRefresh && (
              <LoadingButton
                id={EDIT_IDS.reload}
                variant="secondary"
                size="sm"
                className="btn-sm"
                loading={pending === 'reload'}
                focusableWhileLoading
                disabled={pending !== null && pending !== 'reload'}
                onClick={() => void editor.reload()}
              >
                Reload saved layout
              </LoadingButton>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <h2 id={EDIT_IDS.heading} tabIndex={-1} className="text-sm font-semibold text-fd-ink max-sm:text-xs">
            Customizing dashboard
          </h2>
          {editor.isDirty && (
            <span className="border border-amber-500/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-200">
              Unsaved changes
            </span>
          )}
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            <Button
              id={EDIT_IDS.add}
              variant="secondary"
              size="sm"
              disabled={controlsDisabled}
              onClick={() => editor.openGallery()}
            >
              <PlusIcon aria-hidden="true" className="mr-1 h-4 w-4" />
              Add widgets
            </Button>
            <Button
              id={EDIT_IDS.undo}
              variant="ghost"
              size="sm"
              aria-disabled={undoUnavailable}
              className={undoUnavailable ? 'cursor-not-allowed opacity-50' : undefined}
              onClick={() => {
                if (!undoUnavailable) editor.undo();
              }}
            >
              <ArrowUturnLeftIcon aria-hidden="true" className="h-4 w-4 sm:mr-1" />
              <span className="max-sm:sr-only">Undo</span>
            </Button>
            <Button
              id={EDIT_IDS.reset}
              variant="ghost"
              size="sm"
              disabled={resetDisabled}
              title={editor.hasSavedLayout ? undefined : 'You are already using the default layout'}
              onClick={editor.requestReset}
            >
              Reset<span className="max-sm:sr-only"> to default</span>
            </Button>
            <Button
              id={EDIT_IDS.cancel}
              variant="secondary"
              size="sm"
              disabled={pending !== null}
              onClick={editor.requestCancel}
            >
              Cancel
            </Button>
            <LoadingButton
              id={EDIT_IDS.done}
              size="sm"
              className="btn-sm"
              loading={pending === 'save'}
              focusableWhileLoading
              disabled={controlsDisabled && pending !== 'save'}
              onClick={() => void editor.done()}
            >
              Done
            </LoadingButton>
          </div>
        </div>
        {editor.carriedCount > 0 && <p className="text-xs text-slate-400">{carriedNotice(editor.carriedCount)}</p>}
        {editor.unknownCount > 0 && (
          <p className="text-xs text-amber-200">{unknownWidgetsNotice(editor.unknownCount)}</p>
        )}
        {/* Always mounted, so a notice written into it is announced. */}
        <div role="status" aria-live="polite">
          {pendingNotice ? (
            <p className="text-sm text-slate-300">{pendingNotice}</p>
          ) : (
            editor.notice && <p className="text-sm text-slate-300">{editor.notice}</p>
          )}
        </div>
        <p id={EDIT_IDS.moveHelp} hidden>
          {MOVE_HELP}
        </p>
      </div>

      <ConfirmDialog
        open={editor.confirmResetOpen}
        title="Reset dashboard to default?"
        message="Your saved layout will be replaced by the standard dashboard."
        confirmLabel="Reset to default"
        variant="warning"
        pending={pending === 'reset'}
        onConfirm={() => void editor.reset()}
        onCancel={editor.cancelReset}
      />
    </section>
  );
}

export default EditBar;
