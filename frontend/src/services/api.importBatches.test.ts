import axios from 'axios';
import api from './api';

const originalFetch = global.fetch;
afterEach(() => {
  global.fetch = originalFetch;
  jest.restoreAllMocks();
  sessionStorage.clear();
});

test.each(['prepare', 'commit', 'correct'] as const)(
  '%s never replays a reviewed import under a replacement company token',
  async action => {
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
    const file = new File(['name\nReviewed account'], 'review.csv');
    const pending =
      action === 'prepare'
        ? api.prepareImportBatch('customers', file, 'review-key-12345678')
        : action === 'commit'
          ? api.commitImportBatch(1, 2)
          : api.correctImportBatch(1, 2, file);
    const failed = expect(pending).rejects.toThrow('Session expired');
    api.setTokens('company-B', 'refresh-B', 3600);
    finish({ ok: false, status: 401, json: async () => ({ detail: 'Session expired' }) } as Response);
    await failed;
    expect(transport).toHaveBeenCalledTimes(1);
    expect(transport).toHaveBeenCalledWith(
      expect.stringContaining('/import/batches'),
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer company-A' }),
        body: expect.any(FormData),
      })
    );
    expect(refresh).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('token')).toBe('company-B');
  }
);

test('commit sends its reviewed version and returns the durable receipt', async () => {
  const receipt = { id: 1, version: 3, counts: { created: 1 } };
  const transport = jest.fn().mockResolvedValue({ ok: true, json: async () => receipt });
  global.fetch = transport;
  api.setToken('company-A');
  await expect(api.commitImportBatch(1, 2)).resolves.toEqual(receipt);
  expect(transport.mock.calls[0][1].body.get('expected_version')).toBe('2');
  expect(transport.mock.calls[0][1].headers['Content-Type']).toBeUndefined();
});
