/**
 * copilotChatStream auth behavior.
 *
 * The streaming copilot call uses fetch (axios cannot stream in the browser),
 * which bypasses the axios interceptors — so the method must mirror them
 * itself: proactive refresh when the access token is near expiry, and on a
 * 401 response one refresh + one retry. These tests mock fetch and the axios
 * module boundary (same pattern as api.shipping.test.ts) and drive both paths.
 */

const mockAxiosPost = jest.fn();

const mockAxiosInstance = {
  get: jest.fn(),
  post: jest.fn(),
  put: jest.fn(),
  delete: jest.fn(),
  defaults: { baseURL: 'http://test-api/api/v1', headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: mockAxiosPost },
    create,
    post: mockAxiosPost,
  };
});

type ApiModule = typeof import('./api').default;

/** Build a fake streaming body that replays the given SSE chunks. */
function sseBody(chunks: string[]) {
  const encoder = new TextEncoder();
  let index = 0;
  return {
    getReader: () => ({
      read: async () => {
        if (index < chunks.length) {
          return { done: false, value: encoder.encode(chunks[index++]) };
        }
        return { done: true, value: undefined };
      },
    }),
  };
}

const FINAL_FRAME =
  'data: ' +
  JSON.stringify({
    type: 'final',
    answer: 'Hello from the copilot.',
    references: [],
    tool_trace: [],
    interaction_id: 1,
    rounds: 0,
    truncated: false,
  }) +
  '\n\n';

const okStreamResponse = () => ({ ok: true, status: 200, body: sseBody([FINAL_FRAME]) });

/** (Re)load the api singleton after seeding sessionStorage token state. */
function loadApi(tokenState: { token: string; refreshToken?: string; expiresAt?: number }): ApiModule {
  sessionStorage.clear();
  sessionStorage.setItem('token', tokenState.token);
  if (tokenState.refreshToken) {
    sessionStorage.setItem('refreshToken', tokenState.refreshToken);
  }
  if (tokenState.expiresAt !== undefined) {
    sessionStorage.setItem('tokenExpiresAt', String(tokenState.expiresAt));
  }
  jest.resetModules();
  return require('./api').default as ApiModule;
}

const chatRequest = { messages: [{ role: 'user' as const, content: 'hi' }] };

beforeEach(() => {
  jest.clearAllMocks();
});

describe('copilotChatStream auth refresh', () => {
  it('refreshes once and retries once when the stream endpoint returns 401', async () => {
    const api = loadApi({
      token: 'stale-token',
      refreshToken: 'refresh-1',
      expiresAt: Date.now() + 10 * 60 * 1000, // NOT near expiry — no proactive refresh
    });
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 401, body: null })
      .mockResolvedValueOnce(okStreamResponse());
    global.fetch = fetchMock as unknown as typeof fetch;
    mockAxiosPost.mockResolvedValueOnce({
      data: { access_token: 'fresh-token', refresh_token: 'refresh-2', expires_in: 900 },
    });

    const result = await api.copilotChatStream(chatRequest);

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(mockAxiosPost).toHaveBeenCalledWith(
      expect.stringContaining('/auth/refresh'),
      { refresh_token: 'refresh-1' },
      expect.anything()
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer stale-token');
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe('Bearer fresh-token');
    expect(result.answer).toBe('Hello from the copilot.');
  });

  it('refreshes proactively before fetching when the token is near expiry', async () => {
    const api = loadApi({
      token: 'stale-token',
      refreshToken: 'refresh-1',
      expiresAt: Date.now() + 30 * 1000, // inside the 60s near-expiry window
    });
    const fetchMock = jest.fn().mockResolvedValueOnce(okStreamResponse());
    global.fetch = fetchMock as unknown as typeof fetch;
    mockAxiosPost.mockResolvedValueOnce({
      data: { access_token: 'fresh-token', refresh_token: 'refresh-2', expires_in: 900 },
    });

    const result = await api.copilotChatStream(chatRequest);

    expect(mockAxiosPost).toHaveBeenCalledTimes(1); // refreshed before the stream opened
    expect(fetchMock).toHaveBeenCalledTimes(1); // no retry needed
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer fresh-token');
    expect(result.answer).toBe('Hello from the copilot.');
  });

  it('throws without retrying when a 401 arrives and no refresh token is held', async () => {
    const api = loadApi({ token: 'stale-token', expiresAt: Date.now() + 10 * 60 * 1000 });
    const fetchMock = jest.fn().mockResolvedValue({ ok: false, status: 401, body: null });
    global.fetch = fetchMock as unknown as typeof fetch;

    await expect(api.copilotChatStream(chatRequest)).rejects.toThrow('Copilot request failed (401)');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });
});
