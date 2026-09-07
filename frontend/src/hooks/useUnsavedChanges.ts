import { useCallback, useContext, useEffect, useRef } from 'react';
import { UnsavedChangesContext } from '../context/UnsavedChangesContext';

/** Protect refresh/close, explicit cancel, and SPA navigation through the app's data router.
 * Call markSaved immediately after a successful save before programmatic navigation.
 */
export function useUnsavedChanges(isDirty: boolean, message = 'You have unsaved changes. Discard them?') {
  const register = useContext(UnsavedChangesContext);
  const state = useRef({ isDirty, message, bypass: false });
  state.current.isDirty = isDirty;
  state.current.message = message;
  if (!isDirty) state.current.bypass = false;

  useEffect(
    () =>
      register?.({
        isDirty: () => state.current.isDirty && !state.current.bypass,
        message: () => state.current.message,
      }),
    [register]
  );

  useEffect(() => {
    if (!isDirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (state.current.bypass) return;
      event.preventDefault();
      event.returnValue = '';
      return '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [isDirty]);

  const markSaved = useCallback(() => {
    state.current.bypass = true;
  }, []);
  const confirmDiscard = useCallback(() => {
    if (!state.current.isDirty) return true;
    const allowed = window.confirm(state.current.message);
    if (allowed) {
      state.current.bypass = true;
      // Approval covers this discard/navigation, never later edits left on screen.
      queueMicrotask(() => {
        state.current.bypass = false;
      });
    }
    return allowed;
  }, []);
  return { confirmDiscard, markSaved };
}
export default useUnsavedChanges;
