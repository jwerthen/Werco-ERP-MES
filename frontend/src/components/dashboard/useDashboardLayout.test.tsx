/**
 * The saved-layout hook: a bounded wait, 404/405 = feature not deployed, non-optimistic
 * writes with CAS fields from the last server response, and session-scope changes.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { DashboardLayoutResponse } from '../../types/dashboardLayout';
import {
  DashboardLayoutCompanyChangedError,
  LAYOUT_LOAD_TIMEOUT_MS,
  useDashboardLayout,
  useDashboardSessionScope,
} from './useDashboardLayout';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getDashboardLayout: jest.fn(),
    saveDashboardLayout: jest.fn(),
    resetDashboardLayout: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}

const httpError = (status: number, detail = 'Request failed') =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

const response = (overrides: Partial<DashboardLayoutResponse> = {}): DashboardLayoutResponse => ({
  company_id: 1,
  version: 3,
  layout: { format_version: 1, widgets: [{ id: 'kpi.overdue' }] },
  updated_at: '2026-11-11T15:00:00Z',
  can_edit: true,
  ...overrides,
});

const setup = (scope: string | null = 'scope-a') =>
  renderHook(({ currentScope }: { currentScope: string | null }) => useDashboardLayout(currentScope), {
    initialProps: { currentScope: scope },
  });

beforeEach(() => {
  jest.clearAllMocks();
  jest.useRealTimers();
});

describe('useDashboardLayout', () => {
  it('loads the saved layout', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(response());
    const { result } = setup();
    expect(result.current.status).toBe('loading');
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.saved).toEqual({ format_version: 1, widgets: [{ id: 'kpi.overdue' }] });
    expect(result.current.canEdit).toBe(true);
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledWith(expect.any(AbortSignal));
  });

  it('gives up after the timeout, aborts the request and renders the default', async () => {
    jest.useFakeTimers();
    let signal: AbortSignal | undefined;
    mockedApi.getDashboardLayout.mockImplementation(
      (requestSignal?: AbortSignal) =>
        new Promise(() => {
          signal = requestSignal;
        })
    );
    const { result } = setup();
    await act(async () => {
      jest.advanceTimersByTime(LAYOUT_LOAD_TIMEOUT_MS - 1);
    });
    expect(result.current.status).toBe('loading');
    expect(signal?.aborted).toBe(false);
    await act(async () => {
      jest.advanceTimersByTime(1);
    });
    expect(result.current.status).toBe('error');
    expect(result.current.saved).toBeNull();
    expect(signal?.aborted).toBe(true);
  });

  it('never adopts a superseded request’s answer, whatever order the two settle in', async () => {
    const first = deferred<DashboardLayoutResponse>();
    const second = deferred<DashboardLayoutResponse>();
    mockedApi.getDashboardLayout
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const { result } = setup();
    let reloaded!: Promise<DashboardLayoutResponse | null>;
    act(() => {
      reloaded = result.current.reload();
    });
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(2);

    // The first (superseded) request answers late: it is never swapped in.
    await act(async () => first.resolve(response({ version: 1 })));
    expect(result.current.status).toBe('loading');
    expect(result.current.response).toBeNull();

    await act(async () => second.resolve(response({ version: 2 })));
    expect(result.current.status).toBe('ready');
    expect(result.current.response?.version).toBe(2);
    await expect(reloaded).resolves.toMatchObject({ version: 2 });
  });

  it.each([404, 405])('treats %s as "feature not deployed"', async status => {
    mockedApi.getDashboardLayout.mockRejectedValue(httpError(status));
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('unsupported'));
    expect(result.current.saved).toBeNull();
  });

  it('falls back to the default on any other failure, including a missing api method', async () => {
    mockedApi.getDashboardLayout.mockRejectedValue(httpError(500));
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('error'));

    const original = mockedApi.getDashboardLayout;
    (api as unknown as Record<string, unknown>).getDashboardLayout = undefined;
    try {
      const missing = setup();
      await waitFor(() => expect(missing.result.current.status).toBe('error'));
    } finally {
      (api as unknown as Record<string, unknown>).getDashboardLayout = original;
    }
  });

  it('reloads after an error and becomes ready', async () => {
    mockedApi.getDashboardLayout.mockRejectedValueOnce(httpError(503)).mockResolvedValue(response({ version: 4 }));
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('error'));
    let reloaded: DashboardLayoutResponse | null = null;
    await act(async () => {
      reloaded = await result.current.reload();
    });
    expect(reloaded).toMatchObject({ version: 4 });
    expect(result.current.status).toBe('ready');
  });

  it('saves with CAS fields from the last server response and adopts the new one', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(response({ company_id: 7, version: 3 }));
    const saved = response({ company_id: 7, version: 4, layout: { format_version: 1, widgets: [] } });
    mockedApi.saveDashboardLayout.mockResolvedValue(saved);
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('ready'));

    await act(async () => {
      await result.current.save({ format_version: 1, widgets: [] });
    });
    expect(mockedApi.saveDashboardLayout).toHaveBeenCalledWith({
      expected_company_id: 7,
      expected_version: 3,
      layout: { format_version: 1, widgets: [] },
    });
    expect(result.current.response?.version).toBe(4);
    expect(result.current.saved).toEqual({ format_version: 1, widgets: [] });
  });

  it('rejects a 409 with the server error and keeps the layout it had', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(response());
    const conflict = httpError(409, 'Your dashboard layout changed in another tab. Reload it before saving.');
    mockedApi.saveDashboardLayout.mockRejectedValue(conflict);
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      await expect(result.current.save({ format_version: 1, widgets: [] })).rejects.toBe(conflict);
    });
    expect(result.current.response?.version).toBe(3);
  });

  it('resets through the reset verb', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(response({ version: 5 }));
    mockedApi.resetDashboardLayout.mockResolvedValue(response({ version: 6, layout: null }));
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      await result.current.reset();
    });
    expect(mockedApi.resetDashboardLayout).toHaveBeenCalledWith({ expected_company_id: 1, expected_version: 5 });
    expect(result.current.saved).toBeNull();
  });

  it('keeps a loaded layout on screen when a later reload fails', async () => {
    mockedApi.getDashboardLayout.mockResolvedValueOnce(response({ version: 3 })).mockRejectedValueOnce(httpError(503));
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    let reloaded: DashboardLayoutResponse | null | undefined;
    await act(async () => {
      reloaded = await result.current.reload();
    });
    expect(reloaded).toBeNull();
    expect(result.current.status).toBe('ready');
    expect(result.current.response?.version).toBe(3);
    expect(result.current.saved).toEqual({ format_version: 1, widgets: [{ id: 'kpi.overdue' }] });
  });

  it.each([
    ['an HTML page (a proxy or SPA fallback answering 200)', '<!doctype html><html><body></body></html>'],
    ['an object without the documented fields', { layout: { format_version: 1, widgets: [] } }],
    ['an empty body', null],
  ])('treats %s as a failure, never as a saved layout or an editable session', async (_label, body) => {
    mockedApi.getDashboardLayout.mockResolvedValue(body as never);
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(result.current.saved).toBeNull();
    expect(result.current.canEdit).toBe(false);
    expect(result.current.response).toBeNull();
  });

  it('reads only a boolean true as editable, and a layout without a widget list as the default', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue({
      company_id: 1,
      version: 2,
      layout: { format_version: 1, widgets: 'kpi.overdue' },
      updated_at: 1731337200,
      can_edit: 'true',
    } as never);
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.saved).toBeNull();
    expect(result.current.canEdit).toBe(false);
    expect(result.current.response).toEqual({
      company_id: 1,
      version: 2,
      layout: null,
      updated_at: null,
      can_edit: false,
    });
  });

  it('refuses, and never adopts, a write answer that names a different company', async () => {
    mockedApi.getDashboardLayout.mockResolvedValue(response({ company_id: 1, version: 3 }));
    mockedApi.saveDashboardLayout.mockResolvedValue(response({ company_id: 2, version: 1, layout: null }));
    mockedApi.resetDashboardLayout.mockResolvedValue(response({ company_id: 2, version: 1, layout: null }));
    const { result } = setup();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      await expect(result.current.save({ format_version: 1, widgets: [] })).rejects.toBeInstanceOf(
        DashboardLayoutCompanyChangedError
      );
      await expect(result.current.reset()).rejects.toBeInstanceOf(DashboardLayoutCompanyChangedError);
    });
    expect(result.current.response).toMatchObject({ company_id: 1, version: 3 });
    expect(result.current.saved).toEqual({ format_version: 1, widgets: [{ id: 'kpi.overdue' }] });
  });

  it('never shows a write that settles after the session scope changed', async () => {
    const pendingSave = deferred<DashboardLayoutResponse>();
    const nextLoad = deferred<DashboardLayoutResponse>();
    mockedApi.getDashboardLayout
      .mockResolvedValueOnce(response({ company_id: 1, version: 3 }))
      .mockImplementationOnce(() => nextLoad.promise);
    mockedApi.saveDashboardLayout.mockImplementation(() => pendingSave.promise);
    const { result, rerender } = setup('scope-a');
    await waitFor(() => expect(result.current.status).toBe('ready'));

    let saving!: Promise<DashboardLayoutResponse>;
    act(() => {
      saving = result.current.save({ format_version: 1, widgets: [] });
    });
    rerender({ currentScope: 'scope-b' });
    await act(async () =>
      pendingSave.resolve(response({ company_id: 1, version: 4, layout: { format_version: 1, widgets: [] } }))
    );
    // The caller still learns its write landed; the page never shows company A's answer under B.
    await expect(saving).resolves.toMatchObject({ company_id: 1, version: 4 });
    expect(result.current.status).toBe('loading');
    expect(result.current.response).toBeNull();
    expect(result.current.canEdit).toBe(false);

    await act(async () => nextLoad.resolve(response({ company_id: 2, version: 0, layout: null })));
    expect(result.current.response).toMatchObject({ company_id: 2, version: 0 });
    expect(result.current.saved).toBeNull();
  });

  it('aborts its request when the page unmounts', () => {
    let signal: AbortSignal | undefined;
    mockedApi.getDashboardLayout.mockImplementation((requestSignal?: AbortSignal) => {
      signal = requestSignal;
      return new Promise(() => undefined);
    });
    const { unmount } = setup();
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('re-reads the layout when the session scope changes', async () => {
    mockedApi.getDashboardLayout
      .mockResolvedValueOnce(response({ company_id: 1 }))
      .mockResolvedValueOnce(response({ company_id: 2, version: 0, layout: null, can_edit: false }));
    const { result, rerender } = setup('scope-a');
    await waitFor(() => expect(result.current.response?.company_id).toBe(1));
    rerender({ currentScope: 'scope-b' });
    expect(result.current.status).toBe('loading');
    await waitFor(() => expect(result.current.response?.company_id).toBe(2));
    expect(result.current.saved).toBeNull();
    expect(result.current.canEdit).toBe(false);
    expect(mockedApi.getDashboardLayout).toHaveBeenCalledTimes(2);
  });

  it('reads as loading in the very render the session scope changes — never the previous session’s layout', async () => {
    const next = deferred<DashboardLayoutResponse>();
    mockedApi.getDashboardLayout
      .mockResolvedValueOnce(response({ company_id: 1 }))
      .mockImplementationOnce(() => next.promise);
    const renders: { scope: string | null; status: string; companyId: number | undefined; canEdit: boolean }[] = [];
    const { result, rerender } = renderHook(
      ({ currentScope }: { currentScope: string | null }) => {
        const value = useDashboardLayout(currentScope);
        renders.push({
          scope: currentScope,
          status: value.status,
          companyId: value.response?.company_id,
          canEdit: value.canEdit,
        });
        return value;
      },
      { initialProps: { currentScope: 'scope-a' as string | null } }
    );
    await waitFor(() => expect(result.current.status).toBe('ready'));

    rerender({ currentScope: 'scope-b' });
    const underB = renders.filter(render => render.scope === 'scope-b');
    expect(underB.length).toBeGreaterThan(0);
    expect(underB).toEqual(
      underB.map(() => ({ scope: 'scope-b', status: 'loading', companyId: undefined, canEdit: false }))
    );

    await act(async () => next.resolve(response({ company_id: 2 })));
    expect(result.current.status).toBe('ready');
    expect(result.current.response?.company_id).toBe(2);
  });

  it('refuses to write before the new session’s layout has loaded', async () => {
    const next = deferred<DashboardLayoutResponse>();
    mockedApi.getDashboardLayout
      .mockResolvedValueOnce(response({ company_id: 1 }))
      .mockImplementationOnce(() => next.promise);
    const { result, rerender } = setup('scope-a');
    await waitFor(() => expect(result.current.status).toBe('ready'));
    rerender({ currentScope: 'scope-b' });
    await act(async () => {
      await expect(result.current.save({ format_version: 1, widgets: [] })).rejects.toThrow(/has not loaded yet/);
    });
    expect(mockedApi.saveDashboardLayout).not.toHaveBeenCalled();
    await act(async () => next.resolve(response({ company_id: 2 })));
  });
});

describe('useDashboardSessionScope', () => {
  const tokenFor = (claims: Record<string, unknown>) => `x.${btoa(JSON.stringify(claims))}.y`;

  afterEach(() => sessionStorage.clear());

  it('follows werco:auth-token-changed when the scope (not just the token) changes', () => {
    sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 1, iat: 1 }));
    const { result } = renderHook(() => useDashboardSessionScope());
    const first = result.current;
    expect(first).toContain('"1"');

    act(() => {
      sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 1, iat: 2 }));
      window.dispatchEvent(new Event('werco:auth-token-changed'));
    });
    expect(result.current).toBe(first);

    act(() => {
      sessionStorage.setItem('token', tokenFor({ sub: '1', cid: 2, iat: 3 }));
      window.dispatchEvent(new Event('werco:auth-token-changed'));
    });
    expect(result.current).not.toBe(first);
  });
});
