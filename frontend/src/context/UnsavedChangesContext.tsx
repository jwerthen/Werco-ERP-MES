import React, { createContext, useCallback, useRef } from 'react';
import { useBlocker, BlockerFunction } from 'react-router-dom';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';

type Guard = { isDirty: () => boolean; message: () => string };
type Register = (guard: Guard) => () => void;
export const UnsavedChangesContext = createContext<Register | null>(null);

/** One router blocker aggregates all dirty forms, including nested dialogs. */
export function UnsavedChangesProvider({ children }: { children: React.ReactNode }) {
  const guards = useRef(new Set<Guard>());
  const register = useCallback<Register>(guard => {
    guards.current.add(guard);
    return () => {
      guards.current.delete(guard);
    };
  }, []);
  const blocker = useBlocker(
    useCallback<BlockerFunction>(({ currentLocation, nextLocation }) => {
      if (currentLocation.pathname === nextLocation.pathname && currentLocation.search === nextLocation.search)
        return false;
      return Array.from(guards.current).some(guard => guard.isDirty());
    }, [])
  );
  return (
    <UnsavedChangesContext.Provider value={register}>
      {children}
      <ConfirmDialog
        open={blocker.state === 'blocked'}
        title="Leave with unsaved changes?"
        message="Your changes have not been saved. Stay on this page to keep editing, or leave and discard them."
        confirmLabel="Leave and discard"
        cancelLabel="Stay and keep editing"
        variant="warning"
        onConfirm={() => {
          if (blocker.state === 'blocked') blocker.proceed();
        }}
        onCancel={() => {
          if (blocker.state === 'blocked') blocker.reset();
        }}
      />
    </UnsavedChangesContext.Provider>
  );
}
