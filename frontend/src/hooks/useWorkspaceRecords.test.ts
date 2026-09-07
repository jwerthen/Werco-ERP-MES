import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../services/api';
import { useWorkspaceRecords } from './useWorkspaceRecords';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { listWorkspaceRecords: jest.fn(), saveWorkspaceRecord: jest.fn(), deleteWorkspaceRecord: jest.fn() },
}));
const list = api.listWorkspaceRecords as jest.Mock;
const save = api.saveWorkspaceRecord as jest.Mock;
const record = (version = 1) => ({
  key: 'new',
  namespace: 'work-orders',
  kind: 'draft',
  name: 'Draft',
  data: { note: 'Saved' },
  version,
  updated_at: '2026-09-07T12:00:00',
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  list.mockResolvedValue([]);
});
afterEach(() => sessionStorage.clear());

test('does not fetch without a known account', async () => {
  sessionStorage.clear();
  const { result } = renderHook(() => useWorkspaceRecords('work-orders', 'draft'));
  expect(result.current.rows).toEqual([]);
  expect(list).not.toHaveBeenCalled();
});

test('drops late reads from an account that signed out', async () => {
  const pending = deferred<unknown[]>();
  list.mockReturnValueOnce(pending.promise);
  const { result, rerender } = renderHook(() => useWorkspaceRecords('work-orders', 'draft'));
  sessionStorage.setItem('user', JSON.stringify({ id: 2, company_id: 2 }));
  rerender();
  await act(async () => pending.resolve([record()]));
  expect(result.current.rows).toEqual([]);
  expect(result.current.identity).toBe('2:2');
});

test('a late reload cannot replace a newer completed write', async () => {
  const pending = deferred<unknown[]>();
  list.mockResolvedValueOnce([record()]).mockReturnValueOnce(pending.promise);
  save.mockResolvedValue(record(2));
  const { result } = renderHook(() => useWorkspaceRecords<{ note: string }>('work-orders', 'draft'));
  await waitFor(() => expect(result.current.rows).toHaveLength(1));
  let read!: Promise<unknown>;
  act(() => {
    read = result.current.reload();
  });
  await act(async () => {
    await result.current.save('new', 'Draft', { note: 'Updated' }, 1);
  });
  await act(async () => {
    pending.resolve([record()]);
    await read;
  });
  expect(result.current.rows[0].version).toBe(2);
  expect(result.current.loading).toBe(false);
});

test('failed CAS write keeps the loaded record', async () => {
  list.mockResolvedValue([record()]);
  save.mockRejectedValue({ response: { status: 409 } });
  const { result } = renderHook(() => useWorkspaceRecords('work-orders', 'draft'));
  await waitFor(() => expect(result.current.rows).toHaveLength(1));
  await act(async () => {
    await expect(result.current.save('new', 'Draft', {}, 1)).rejects.toEqual({ response: { status: 409 } });
  });
  expect(result.current.rows[0].version).toBe(1);
});
