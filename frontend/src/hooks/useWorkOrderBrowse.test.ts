import { act, renderHook, waitFor } from '@testing-library/react';
import api from '../services/api';
import { useWorkOrderBrowse } from './useWorkOrderBrowse';
import { WorkOrderBrowseResponse } from '../types/workOrderBrowse';
import { WorkOrderSummary } from '../types';
jest.mock('../services/api', () => ({ __esModule: true, default: { browseWorkOrders: jest.fn() } }));
const request = api.browseWorkOrders as jest.Mock;
const response = (ids: number[], total = 123, skip = 0): WorkOrderBrowseResponse => ({
  items: ids.map(id => ({ id, work_order_number: `WO-${id}` }) as WorkOrderSummary),
  total,
  skip,
  limit: 50,
  has_next: skip + ids.length < total,
  stats: { overdue: 70, in_progress: 20, due_today: 4 },
  customers: ['Acme'],
  customers_truncated: false,
  group_totals: {},
});
const ids = (start: number, end: number) => Array.from({ length: end - start }, (_, i) => start + i);
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
});
afterEach(() => sessionStorage.clear());

test('initial request bounded; mobile increment and refresh touch only requested windows', async () => {
  request.mockImplementation(({ skip }) => Promise.resolve(response(ids(skip, Math.min(123, skip + 50)), 123, skip)));
  const { result } = renderHook(() => useWorkOrderBrowse({ customer: 'Acme' }));
  await act(async () => {
    await result.current.loadWorkOrders();
  });
  expect(request).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenLastCalledWith({ customer: 'Acme', skip: 0, limit: 50 });
  expect(result.current.total).toBe(123);
  expect(result.current.workOrders).toHaveLength(50);
  act(() => result.current.loadMore());
  await waitFor(() => expect(result.current.workOrders).toHaveLength(100));
  expect(request).toHaveBeenLastCalledWith({ customer: 'Acme', skip: 50, limit: 50 });
  request.mockClear();
  await act(async () => {
    await result.current.loadWorkOrders();
  });
  expect(request).toHaveBeenCalledTimes(2);
  expect(result.current.workOrders).toHaveLength(100);
});

test('new filter instantly hides old rows, drops late responses and resets offset', async () => {
  const pending = deferred<WorkOrderBrowseResponse>();
  request
    .mockResolvedValueOnce(response([1]))
    .mockReturnValueOnce(pending.promise)
    .mockResolvedValueOnce(response([3], 1));
  const { result, rerender } = renderHook(({ customer }) => useWorkOrderBrowse({ customer }), {
    initialProps: { customer: 'Old' },
  });
  await act(async () => {
    await result.current.loadWorkOrders();
  });
  act(() => result.current.loadMore());
  rerender({ customer: 'New' });
  expect(result.current.workOrders).toEqual([]);
  await act(async () => {
    await result.current.loadWorkOrders();
  });
  await act(async () => {
    pending.resolve(response([2], 123, 50));
  });
  expect(result.current.workOrders.map(row => row.id)).toEqual([3]);
  expect(request).toHaveBeenLastCalledWith({ customer: 'New', skip: 0, limit: 50 });
});

test('failed next page preserves rows and retry offset; duplicate load clicks issue one request', async () => {
  request
    .mockResolvedValueOnce(response(ids(0, 50)))
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(response(ids(50, 100), 123, 50));
  const { result } = renderHook(() => useWorkOrderBrowse({}));
  await act(async () => {
    await result.current.loadWorkOrders();
  });
  act(() => {
    result.current.loadMore();
    result.current.loadMore();
  });
  await waitFor(() => expect(result.current.loadError).toBe(true));
  expect(result.current.workOrders).toHaveLength(50);
  expect(request).toHaveBeenCalledTimes(2);
  act(() => result.current.loadMore());
  await waitFor(() => expect(result.current.workOrders).toHaveLength(100));
  expect(request).toHaveBeenLastCalledWith({ skip: 50, limit: 50 });
});

test('late reads after account change cannot reveal previous company rows', async () => {
  const pending = deferred<WorkOrderBrowseResponse>();
  request.mockReturnValue(pending.promise);
  const { result, rerender } = renderHook(() => useWorkOrderBrowse({}));
  act(() => {
    void result.current.loadWorkOrders();
  });
  sessionStorage.setItem('user', JSON.stringify({ id: 2, company_id: 2 }));
  rerender();
  await act(async () => pending.resolve(response([99])));
  expect(result.current.workOrders).toEqual([]);
});
