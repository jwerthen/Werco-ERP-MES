/**
 * The viewer's saved dashboard layout (`GET/PUT /dashboard/layout`, `POST .../reset`).
 *
 * The layout must never hold the landing page hostage:
 * - the GET is aborted after LAYOUT_LOAD_TIMEOUT_MS (3 s) — status `'error'`, and the
 *   page renders the stock default. A late answer is never swapped in under the user (the
 *   request was aborted; a newer request also supersedes an older one);
 * - 404/405 mean the API predates the feature (the SPA and API deploy independently):
 *   status `'unsupported'`, the default renders and Customize is hidden;
 * - a missing api method, a synchronous throw or any other failure is status `'error'`.
 *
 * Writes are non-optimistic and send `expected_company_id` / `expected_version` from the
 * LAST SERVER RESPONSE (the Hank-preferences precedent), never from a client company
 * context. Failures reject with the original error so the caller can show the server's
 * `detail` (409 = changed in another tab).
 *
 * The hook re-runs its GET whenever the session scope (user, active company, read-only
 * flag, token scope/type) changes — `useDashboardSessionScope`.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import type { DashboardLayoutResponse, DashboardLayoutValues } from '../../types/dashboardLayout';
import { apiErrorStatus } from '../../utils/apiError';
import { getHankSessionScope, subscribeHankSession } from '../ai/hankSession';

export const LAYOUT_LOAD_TIMEOUT_MS = 3000;

export type DashboardLayoutStatus = 'loading' | 'ready' | 'error' | 'unsupported';

export interface DashboardLayoutState {
  status: DashboardLayoutStatus;
  /** The last server response (null until one arrives, and after a scope change). */
  response: DashboardLayoutResponse | null;
}

export interface UseDashboardLayoutResult {
  status: DashboardLayoutStatus;
  response: DashboardLayoutResponse | null;
  /** The saved layout; null = the stock default (also while loading / on error / unsupported). */
  saved: DashboardLayoutValues | null;
  canEdit: boolean;
  /** Re-run the GET without blanking the page. Resolves with the response, or null when it failed. */
  reload: () => Promise<DashboardLayoutResponse | null>;
  /** Save a customized layout. Rejects with the request error (409 = stale version). */
  save: (layout: DashboardLayoutValues) => Promise<DashboardLayoutResponse>;
  /** Return to the stock default. Rejects with the request error. */
  reset: () => Promise<DashboardLayoutResponse>;
}

/** The session scope string; re-renders when a token change alters it. */
export function useDashboardSessionScope(): string | null {
  const [scope, setScope] = useState<string | null>(() => getHankSessionScope());
  useEffect(() => {
    // Re-read once on mount (the token may have rotated between render and effect).
    setScope(getHankSessionScope());
    return subscribeHankSession(() => setScope(getHankSessionScope()));
  }, []);
  return scope;
}

/** Keep a server answer only when it has the documented shape; anything else reads as "the default". */
function normalizeResponse(raw: unknown): DashboardLayoutResponse | null {
  if (!raw || typeof raw !== 'object') return null;
  const response = raw as Partial<DashboardLayoutResponse>;
  if (typeof response.company_id !== 'number' || typeof response.version !== 'number') return null;
  const layout = response.layout;
  const validLayout =
    layout && typeof layout === 'object' && Array.isArray((layout as DashboardLayoutValues).widgets)
      ? (layout as DashboardLayoutValues)
      : null;
  return {
    company_id: response.company_id,
    version: response.version,
    layout: validLayout,
    updated_at: typeof response.updated_at === 'string' ? response.updated_at : null,
    can_edit: response.can_edit === true,
  };
}

export class DashboardLayoutCompanyChangedError extends Error {
  constructor() {
    super('Your active company changed. Reload the dashboard before saving its layout.');
    this.name = 'DashboardLayoutCompanyChangedError';
  }
}

export function useDashboardLayout(scope: string | null): UseDashboardLayoutResult {
  const [state, setState] = useState<DashboardLayoutState>({ status: 'loading', response: null });
  const stateRef = useRef(state);
  stateRef.current = state;
  const seqRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  const load = useCallback(async (): Promise<DashboardLayoutResponse | null> => {
    const seq = ++seqRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Race the request against the timer rather than trusting the abort alone: whatever
    // the transport does with the signal, the page stops waiting at the deadline.
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error('Dashboard layout request timed out'));
      }, LAYOUT_LOAD_TIMEOUT_MS);
    });
    const isCurrent = () => mountedRef.current && seq === seqRef.current;
    try {
      const raw = await Promise.race([api.getDashboardLayout(controller.signal), deadline]);
      if (!isCurrent()) return null;
      const response = normalizeResponse(raw);
      if (!response) throw new Error('Unexpected dashboard layout response');
      setState({ status: 'ready', response });
      return response;
    } catch (error) {
      if (!isCurrent()) return null;
      const status = apiErrorStatus(error);
      if (status === 404 || status === 405) {
        setState({ status: 'unsupported', response: null });
      } else if (stateRef.current.status !== 'ready') {
        // A failed RE-load never downgrades a layout already on screen.
        setState({ status: 'error', response: null });
      }
      return null;
    } finally {
      clearTimeout(timer);
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      ++seqRef.current;
      controllerRef.current?.abort();
    };
  }, []);

  // Initial load, and a fresh load on every session-scope change.
  useEffect(() => {
    // Never keep showing the previous session's layout while the new one loads.
    setState(previous =>
      previous.status === 'loading' && previous.response === null ? previous : { status: 'loading', response: null }
    );
    void load();
  }, [scope, load]);

  const write = useCallback(async (send: (current: DashboardLayoutResponse) => Promise<DashboardLayoutResponse>) => {
    const current = stateRef.current.response;
    if (!current) throw new Error('Your saved dashboard layout has not loaded yet. Reload it and try again.');
    const seq = seqRef.current;
    const raw = await send(current);
    const response = normalizeResponse(raw);
    if (!response) throw new Error('Unexpected dashboard layout response');
    if (response.company_id !== current.company_id) throw new DashboardLayoutCompanyChangedError();
    if (mountedRef.current && seq === seqRef.current) setState({ status: 'ready', response });
    return response;
  }, []);

  const save = useCallback(
    (layout: DashboardLayoutValues) =>
      write(current =>
        api.saveDashboardLayout({
          expected_company_id: current.company_id,
          expected_version: current.version,
          layout,
        })
      ),
    [write]
  );

  const reset = useCallback(
    () =>
      write(current =>
        api.resetDashboardLayout({ expected_company_id: current.company_id, expected_version: current.version })
      ),
    [write]
  );

  return {
    status: state.status,
    response: state.response,
    saved: state.response?.layout ?? null,
    canEdit: state.response?.can_edit === true,
    reload: load,
    save,
    reset,
  };
}
