import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../services/api';
import { normalizeLayout, useTableWorkspace } from './useTableWorkspace';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    listTeamWorkspaceRecords: jest.fn(),
    saveTeamWorkspaceRecord: jest.fn(),
    deleteTeamWorkspaceRecord: jest.fn(),
    listWorkspaceRecords: jest.fn(),
    saveWorkspaceRecord: jest.fn(),
    deleteWorkspaceRecord: jest.fn(),
  },
}));
const list = api.listWorkspaceRecords as jest.Mock;
const save = api.saveWorkspaceRecord as jest.Mock;
const columns = [
  { key: 'id', header: 'Order', accessor: (row: { id: number }) => row.id, sortable: true },
  { key: 'status', header: 'Status' },
  { key: 'actions', header: 'Actions' },
];
const layout = {
  order: ['status', 'id', 'actions'],
  hidden: ['status'],
  dense: true,
  sort: { key: 'id', dir: 'desc' as const },
};
const view = {
  key: 'orders-priority',
  namespace: 'work-orders',
  kind: 'view',
  name: 'Priority',
  data: { table: 'orders', layout, filters: { status: 'released' } },
  version: 1,
  updated_at: '2026-09-07T12:00:00',
};

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  list.mockResolvedValue([view]);
  (api.listTeamWorkspaceRecords as jest.Mock).mockResolvedValue({ items: [], can_manage: false });
  save.mockImplementation((_ns, key, value) => Promise.resolve({ ...view, ...value, key, version: 1 }));
});
afterEach(() => sessionStorage.clear());

test('normalizes obsolete/duplicate columns and protects identity and actions', () => {
  expect(
    normalizeLayout(
      { order: ['obsolete', 'status', 'status'], hidden: ['id', 'actions', 'status', 'obsolete'] },
      columns,
      null
    )
  ).toEqual({ order: ['status', 'id', 'actions'], hidden: ['status'], dense: false, sort: null });
});

test('saved layout loads without overwriting filters from the current deep link', async () => {
  list.mockResolvedValue([{ ...view, key: 'orders-layout' }]);
  const apply = jest.fn();
  const { result } = renderHook(() =>
    useTableWorkspace('work-orders', 'orders', columns, { status: 'on_hold' }, apply)
  );
  await waitFor(() => expect(result.current.layout.dense).toBe(true));
  expect(apply).not.toHaveBeenCalled();
  expect(result.current.views).toHaveLength(0);
});

test('named view applies filters and layout only when requested', async () => {
  const apply = jest.fn();
  const { result } = renderHook(() => useTableWorkspace('work-orders', 'orders', columns, {}, apply));
  await waitFor(() => expect(result.current.views).toHaveLength(1));
  expect(result.current.layout.dense).toBe(false);
  act(() => result.current.apply(view.key));
  expect(apply).toHaveBeenCalledWith({ status: 'released' });
  expect(result.current.displayColumns(columns).map(column => column.key)).toEqual(['id', 'actions']);
});

test('table layout save excludes record filters and uses observed version', async () => {
  list.mockResolvedValue([{ ...view, key: 'orders-layout', version: 4 }]);
  const { result } = renderHook(() =>
    useTableWorkspace('work-orders', 'orders', columns, { status: 'released', id: '77' }, jest.fn())
  );
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => {
    await result.current.saveLayout();
  });
  expect(save).toHaveBeenCalledWith(
    'work-orders',
    'orders-layout',
    expect.objectContaining({ version: 4, data: expect.objectContaining({ filters: {} }) })
  );
});

test('team view is readable but immutable for a permitted non-manager', async () => {
  (api.listTeamWorkspaceRecords as jest.Mock).mockResolvedValue({ items: [view], can_manage: false });
  const apply = jest.fn();
  const { result } = renderHook(() => useTableWorkspace('work-orders', 'orders', columns, {}, apply));
  await waitFor(() => expect(result.current.views).toHaveLength(2));
  act(() => result.current.apply(`team:${view.key}`));
  expect(apply).toHaveBeenCalledWith(view.data.filters);
  expect(result.current.canEditView(`team:${view.key}`)).toBe(false);
  await act(async () => {
    await result.current.updateView(`team:${view.key}`);
  });
  expect(api.saveTeamWorkspaceRecord).not.toHaveBeenCalled();
});

test('manager updates observed team version without mutating private view with same key', async () => {
  (api.listTeamWorkspaceRecords as jest.Mock).mockResolvedValue({ items: [{ ...view, version: 4 }], can_manage: true });
  (api.saveTeamWorkspaceRecord as jest.Mock).mockImplementation((_ns, key, payload) =>
    Promise.resolve({ ...view, ...payload, key, version: 5 })
  );
  const { result } = renderHook(() =>
    useTableWorkspace('work-orders', 'orders', columns, { status: 'on_hold' }, jest.fn())
  );
  await waitFor(() => expect(result.current.canManageTeam).toBe(true));
  await act(async () => {
    await result.current.updateView(`team:${view.key}`);
  });
  expect(api.saveTeamWorkspaceRecord).toHaveBeenCalledWith(
    'work-orders',
    view.key,
    expect.objectContaining({
      kind: 'view',
      version: 4,
      data: expect.objectContaining({ filters: { status: 'on_hold' } }),
    })
  );
  expect(save).not.toHaveBeenCalled();
  expect(result.current.views.find(row => row.visibility === 'private')?.version).toBe(1);
});

test('explicit legacy import saves those filters privately without applying or sharing them', async () => {
  const apply = jest.fn();
  const { result } = renderHook(() => useTableWorkspace('parts', 'catalog', columns, { search: 'current' }, apply));
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => {
    await result.current.saveView('Device import', 'private', { search: 'legacy' });
  });
  expect(save).toHaveBeenCalledWith(
    'parts',
    expect.any(String),
    expect.objectContaining({
      kind: 'view',
      name: 'Device import',
      data: expect.objectContaining({ filters: { search: 'legacy' } }),
    })
  );
  expect(apply).not.toHaveBeenCalled();
  expect(api.saveTeamWorkspaceRecord).not.toHaveBeenCalled();
});

test('late team write after an account switch cannot replace new account views or status', async () => {
  let finish!: (value: unknown) => void;
  (api.listTeamWorkspaceRecords as jest.Mock).mockResolvedValue({ items: [], can_manage: true });
  (api.saveTeamWorkspaceRecord as jest.Mock).mockImplementation(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const { result, rerender } = renderHook(() => useTableWorkspace('work-orders', 'orders', columns, {}, jest.fn()));
  await waitFor(() => expect(result.current.loading).toBe(false));
  let saving!: Promise<void>;
  act(() => {
    saving = result.current.saveView('Old company name', 'team');
  });
  sessionStorage.setItem('user', JSON.stringify({ id: 2, company_id: 2 }));
  list.mockResolvedValue([]);
  rerender();
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => {
    finish({ ...view, key: 'late', name: 'Old company name' });
    await saving;
  });
  expect(result.current.views).toEqual([]);
  expect(result.current.message).toBe('');
  expect(result.current.busy).toBe(false);
});
