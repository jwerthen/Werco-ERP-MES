/**
 * The saved-layout hook: a bounded wait, 404/405 = feature not deployed, non-optimistic
 * writes with CAS fields from the last server response, and session-scope changes.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { DashboardLayoutResponse } from '../../types/dashboardLayout';
import { LAYOUT_LOAD_TIMEOUT_MS, useDashboardLayout, useDashboardSessionScope } from './useDashboardLayout';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getDashboardLayout: jest.fn(),
    saveDashboardLayout: jest.fn(),
    resetDashboardLayout: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

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

  it('gives up after the timeout, renders the default, and never swaps a late answer in', async () => {
    jest.useFakeTimers();
    let resolveLate!: (value: DashboardLayoutResponse) => void;
    let signal: AbortSignal | undefined;
    mockedApi.getDashboardLayout.mockImplementation(
      (requestSignal?: AbortSignal) =>
        new Promise(resolve => {
          signal = requestSignal;
          resolveLate = resolve;
        })
    );
    const { result } = setup();
    await act(async () => {
      jest.advanceTimersByTime(LAYOUT_LOAD_TIMEOUT_MS - 1);
    });
    expect(result.current.status).toBe('loading');
    await act(async () => {
      jest.advanceTimersByTime(1);
    });
    expect(result.current.status).toBe('error');
    expect(result.current.saved).toBeNull();
    expect(signal?.aborted).toBe(true);

    await act(async () => resolveLate(response()));
    expect(result.current.status).toBe('error');
    expect(result.current.saved).toBeNull();
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
