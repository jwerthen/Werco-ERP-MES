import axios from 'axios';
import api from './api';

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
  jest.restoreAllMocks();
  sessionStorage.clear();
});

test.each(['clear', 'pause'] as const)('%s keeps the original company token after a delayed 401', async action => {
  const refresh = jest.spyOn(axios, 'post').mockRejectedValue(new Error('Unexpected refresh'));
  let finish!: (response: Response) => void;
  const transport = jest.fn(
    () =>
      new Promise<Response>(resolve => {
        finish = resolve;
      })
  );
  global.fetch = transport;
  api.setTokens('company-A', 'refresh-A', 3600);
  const pending = action === 'clear' ? api.clearRuntimeMetrics() : api.setRuntimeMetricsEnabled(false);
  const failed = expect(pending).rejects.toThrow('Refresh and try again');
  api.setTokens('company-B', 'refresh-B', 3600);
  finish({ ok: false, status: 401 } as Response);
  await failed;
  expect(transport).toHaveBeenCalledTimes(1);
  expect(transport).toHaveBeenCalledWith(
    expect.stringContaining('/runtime-metrics/'),
    expect.objectContaining({
      method: action === 'clear' ? 'DELETE' : 'PUT',
      headers: expect.objectContaining({ Authorization: 'Bearer company-A' }),
    })
  );
  expect(refresh).not.toHaveBeenCalled();
  expect(sessionStorage.getItem('token')).toBe('company-B');
});

test('pause and clear preserve their server contracts without a refresh transport', async () => {
  const transport = jest
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ enabled: false, retention_days: 30 }),
    })
    .mockResolvedValueOnce({ ok: true, status: 204 });
  global.fetch = transport;
  api.setToken('company-A');
  await expect(api.setRuntimeMetricsEnabled(false)).resolves.toEqual({ enabled: false, retention_days: 30 });
  await expect(api.clearRuntimeMetrics()).resolves.toBeUndefined();
  expect(transport.mock.calls[0][1].body).toBe(JSON.stringify({ enabled: false }));
  expect(transport.mock.calls[1][1].body).toBeUndefined();
});
