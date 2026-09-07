import { act, renderHook } from '@testing-library/react';
import { useProductionReportRequest } from './useProductionReportRequest';

const body = { quantity_complete_delta: 3, quantity_scrapped_delta: 1, scrap_reason: 'Porosity', source: 'kiosk' };

test('unknown outcomes retry the original request and body, and the next deliberate report gets a new ID', async () => {
  const post = jest.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValue({ replayed: true });
  const { result } = renderHook(useProductionReportRequest);
  await act(async () => {
    await expect(result.current.submit(7, 31, body, post)).rejects.toThrow('response lost');
  });
  const original = post.mock.calls[0][0];
  expect(original.request_id).toEqual(expect.any(String));
  expect(result.current.unconfirmed?.body).toEqual(original);
  await act(async () => {
    await result.current.retry(7, (_operationId, data) => post(data));
  });
  expect(post.mock.calls[1][0]).toEqual(original);
  expect(result.current.unconfirmed).toBeNull();
  await act(async () => {
    await result.current.submit(7, 31, body, post);
  });
  expect(post.mock.calls[2][0].request_id).not.toBe(original.request_id);
});

test('edited fields or another actor cannot silently replace an uncertain report', async () => {
  const post = jest.fn().mockRejectedValue(new Error('response lost'));
  const { result } = renderHook(useProductionReportRequest);
  await act(async () => {
    await result.current.submit(7, 31, body, post).catch(() => undefined);
  });
  await act(async () => {
    await expect(result.current.submit(7, 31, { ...body, quantity_complete_delta: 4 }, post)).rejects.toThrow(
      'earlier production report'
    );
    await expect(result.current.submit(8, 31, body, post)).rejects.toThrow('earlier production report');
    await expect(result.current.retry(8, post)).rejects.toThrow('original operator');
  });
  expect(post).toHaveBeenCalledTimes(1);
  expect(result.current.unconfirmed?.body.quantity_complete_delta).toBe(3);
});

test('badge expiry after an unknown outcome does not discard the original request identity', async () => {
  const post = jest
    .fn()
    .mockRejectedValueOnce(new Error('response lost'))
    .mockRejectedValueOnce({ status: 401 })
    .mockResolvedValue({ replayed: true });
  const { result } = renderHook(useProductionReportRequest);
  await act(async () => {
    await result.current.submit(7, 31, body, post).catch(() => undefined);
  });
  await act(async () => {
    await result.current.retry(7, (_id, data) => post(data)).catch(() => undefined);
  });
  expect(result.current.unconfirmed).not.toBeNull();
  await act(async () => {
    await result.current.submit(7, 31, body, post);
  });
  expect(post.mock.calls.map(call => call[0].request_id)).toEqual(Array(3).fill(post.mock.calls[0][0].request_id));
});

test('an initial definitive refusal allows corrected input and a fresh request', async () => {
  const post = jest
    .fn()
    .mockRejectedValueOnce({ response: { status: 400 } })
    .mockResolvedValue({});
  const { result } = renderHook(useProductionReportRequest);
  await act(async () => {
    await result.current.submit(7, 31, body, post).catch(() => undefined);
  });
  expect(result.current.unconfirmed).toBeNull();
  await act(async () => {
    await result.current.submit(7, 31, { ...body, quantity_complete_delta: 2 }, post);
  });
  expect(post.mock.calls[1][0].request_id).not.toBe(post.mock.calls[0][0].request_id);
});

test('duplicate pending events send only one request', async () => {
  let done!: (value: unknown) => void;
  const post = jest.fn(
    () =>
      new Promise(resolve => {
        done = resolve;
      })
  );
  const { result } = renderHook(useProductionReportRequest);
  let first!: Promise<unknown>;
  act(() => {
    first = result.current.submit(7, 31, body, post);
  });
  await act(async () => {
    await expect(result.current.submit(7, 31, body, post)).rejects.toThrow('already being checked');
  });
  await act(async () => {
    done({});
    await first;
  });
  expect(post).toHaveBeenCalledTimes(1);
});

test('reload recovers the exact original report without retaining credentials or allowing a different operator', async () => {
  const key = 'production-reload-regression';
  sessionStorage.removeItem(key);
  const post = jest.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValue({ replayed: true });
  const first = renderHook(() => useProductionReportRequest(key));
  await act(async () => {
    await first.result.current.submit(7, 31, body, post).catch(() => undefined);
  });
  const original = post.mock.calls[0][0];
  first.unmount();
  const restored = renderHook(() => useProductionReportRequest(key));
  expect(restored.result.current.unconfirmed).toEqual({ operatorId: 7, operationId: 31, body: original });
  await act(async () => {
    await expect(restored.result.current.retry(8, post)).rejects.toThrow('original operator');
  });
  await act(async () => {
    await restored.result.current.retry(7, (_id, data) => post(data));
  });
  expect(post.mock.calls[1][0]).toEqual(original);
  expect(sessionStorage.getItem(key)).toBeNull();
});
