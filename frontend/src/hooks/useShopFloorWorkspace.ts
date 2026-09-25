import { useCallback, useEffect, useMemo, useState } from 'react';

export type ShopFloorView = 'my-work' | 'ready' | 'all';

export interface ShopFloorWorkspace {
  selectedOperationId: number | null;
  activeTimeEntryId: number | null;
  view: ShopFloorView;
  workCenterId: number | null;
  savedAt: number | null;
}

type WorkspaceUpdate = Partial<Omit<ShopFloorWorkspace, 'savedAt'>>;
type WorkspaceUser = { id: number; company_id?: number } | null | undefined;

const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const emptyWorkspace = (): ShopFloorWorkspace => ({
  selectedOperationId: null,
  activeTimeEntryId: null,
  view: 'my-work',
  workCenterId: null,
  savedAt: null,
});
const validId = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;
const validView = (value: unknown): value is ShopFloorView =>
  value === 'my-work' || value === 'ready' || value === 'all';

function workspaceKey(user: WorkspaceUser): string | null {
  return user && validId(user.id) && validId(user.company_id)
    ? `shop_floor_workspace:v1:company:${user.company_id}:user:${user.id}`
    : null;
}

function readWorkspace(key: string | null): ShopFloorWorkspace {
  if (!key) return emptyWorkspace();
  try {
    const stored = sessionStorage.getItem(key);
    if (!stored) return emptyWorkspace();
    const value = JSON.parse(stored);
    if (
      !value ||
      !Number.isFinite(value.savedAt) ||
      value.savedAt > Date.now() ||
      Date.now() - value.savedAt > MAX_AGE_MS ||
      !validView(value.view) ||
      ![value.selectedOperationId, value.activeTimeEntryId, value.workCenterId].every(id => id === null || validId(id))
    ) {
      return emptyWorkspace();
    }
    return {
      selectedOperationId: value.selectedOperationId,
      activeTimeEntryId: value.activeTimeEntryId,
      view: value.view,
      workCenterId: value.workCenterId,
      savedAt: value.savedAt,
    };
  } catch {
    return emptyWorkspace();
  }
}

/**
 * Resume the same operator's screen after idle sign-in in this browser tab.
 * Store IDs and view preferences only; never production drafts or credentials.
 * Callers must revalidate saved operation/time-entry IDs against freshly loaded
 * active jobs before opening them. Unknown identities do not read or persist.
 */
export function useShopFloorWorkspace(user: WorkspaceUser) {
  const key = workspaceKey(user);
  const restoredWorkspace = useMemo(() => readWorkspace(key), [key]);
  const [state, setState] = useState(() => ({ key, workspace: restoredWorkspace }));
  // Do not expose the previous operator's state for even one render on a switch.
  const workspace = state.key === key ? state.workspace : restoredWorkspace;

  useEffect(() => {
    setState({ key, workspace: restoredWorkspace });
  }, [key, restoredWorkspace]);

  const updateWorkspace = useCallback(
    (update: WorkspaceUpdate) => {
      if (!key) return;
      setState(current => {
        const previous = current.key === key ? current.workspace : readWorkspace(key);
        const next = { ...previous, ...update, savedAt: Date.now() };
        // Runtime checks protect persisted state if an API payload carries bad IDs.
        if (
          !validView(next.view) ||
          ![next.selectedOperationId, next.activeTimeEntryId, next.workCenterId].every(id => id === null || validId(id))
        ) {
          return current;
        }
        try {
          sessionStorage.setItem(key, JSON.stringify(next));
        } catch {
          // The operator can still work when private browsing/storage quotas deny writes.
        }
        return { key, workspace: next };
      });
    },
    [key]
  );

  const clearWorkspace = useCallback(() => {
    if (!key) return;
    try {
      sessionStorage.removeItem(key);
    } catch {
      // Reset remains usable for this visit without browser storage.
    }
    setState({ key, workspace: emptyWorkspace() });
  }, [key]);

  return { workspace, updateWorkspace, clearWorkspace };
}
