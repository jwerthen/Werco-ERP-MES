import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../services/api';
import { normalizeLayout, useTableWorkspace } from './useTableWorkspace';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { listWorkspaceRecords: jest.fn(), saveWorkspaceRecord: jest.fn(), deleteWorkspaceRecord: jest.fn() },
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
