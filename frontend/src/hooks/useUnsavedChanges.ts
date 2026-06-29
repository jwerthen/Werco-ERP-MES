import { useCallback, useEffect, useRef } from 'react';

/**
 * useUnsavedChanges — guards against losing in-progress edits.
 *
 * Two complementary guards:
 *
 *   1. While `isDirty` is true, a `beforeunload` handler is registered so the
 *      browser prompts the user on refresh, tab-close, or window-close. The
 *      listener is removed as soon as the form is clean again (or the component
 *      unmounts), so a saved form never blocks navigation.
 *
 *   2. `confirmDiscard()` is for the form's own Cancel/Close affordances: it
 *      returns `true` immediately when the form is clean, and otherwise pops a
 *      `window.confirm` and returns the user's choice. Wire it as a gate before
 *      discarding state — e.g. `onClick={() => { if (confirmDiscard()) close(); }}`.
 *
 * IN-APP NAV BLOCKING IS UNAVAILABLE in this app. The router is mounted as the
 * component `<BrowserRouter>` (see src/App.tsx), not a data router created via
 * `createBrowserRouter`, so react-router's `useBlocker` / `unstable_usePrompt`
 * have no Navigation context to hook into and will throw. Client-side route
 * changes therefore cannot be intercepted here; this hook deliberately covers
 * only the `beforeunload` (refresh/close) path plus the explicit Cancel/Close
 * gate. Switching to a data router would be required to add SPA-nav blocking,
 * and that is out of scope for this accessibility/UX pass.
 *
 * @param isDirty Whether the form currently has unsaved changes.
 * @param message Optional prompt text for the Cancel/Close confirm. (Modern
 *   browsers ignore custom text on the native beforeunload dialog and show their
 *   own generic message; the custom text is used only by `confirmDiscard`.)
 */
export function useUnsavedChanges(
  isDirty: boolean,
  message = 'You have unsaved changes. Discard them?'
) {
  // Keep the latest message in a ref so the confirm handler stays stable
  // (no need to re-create it when only the message text changes).
  const messageRef = useRef(message);
  messageRef.current = message;

  useEffect(() => {
    if (!isDirty) return;

    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      // Triggering the native prompt requires both preventDefault() and a
      // non-empty returnValue across browsers.
      event.preventDefault();
      event.returnValue = '';
      return '';
    };

    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [isDirty]);

  /**
   * Returns true if it is safe to discard (clean form, or the user confirmed).
   * Call this from Cancel/Close handlers before tearing down form state.
   */
  const confirmDiscard = useCallback((): boolean => {
    if (!isDirty) return true;
    return window.confirm(messageRef.current);
  }, [isDirty]);

  return { confirmDiscard };
}

export default useUnsavedChanges;
