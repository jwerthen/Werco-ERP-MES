import { createSourcePacer, sourceRetryAt } from './cadSourceTransport';

test('batch write slots are at least800ms apart and a cancelled wait cannot issue another request', async () => {
  jest.useFakeTimers();
  try {
    const pace = createSourcePacer();
    const controller = new AbortController();
    await pace(controller.signal);
    const done = jest.fn();
    const waiting = pace(controller.signal).then(done);
    await jest.advanceTimersByTimeAsync(799);
    expect(done).not.toHaveBeenCalled();
    await jest.advanceTimersByTimeAsync(1);
    await waiting;
    expect(done).toHaveBeenCalledTimes(1);
    const cancelled = pace(controller.signal);
    const rejection = expect(cancelled).rejects.toThrow('stopped');
    controller.abort();
    await rejection;
  } finally {
    jest.useRealTimers();
  }
});

test('Retry-After seconds or HTTP date produce explicit resume times; malformed values fall back60s', () => {
  const now = Date.parse('2026-09-08T18:00:00Z');
  const error = (value: string) => ({ response: { status: 429, headers: { 'retry-after': value } } });
  expect(sourceRetryAt(error('15'), now)).toBe(now + 15_000);
  expect(sourceRetryAt(error('Tue, 08 Sep 2026 18:01:00 GMT'), now)).toBe(now + 60_000);
  expect(sourceRetryAt(error('invalid'), now)).toBe(now + 60_000);
  expect(sourceRetryAt({ response: { status: 503 } }, now)).toBeNull();
});
