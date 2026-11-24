/**
 * Self-service password change client boundary.
 *
 * `api.changeOwnPassword` posts `{current_password, new_password}` to
 * `POST /users/change-password` and returns the server's `{message}` body. The
 * component suite mocks this module, so the URL and body shape are pinned here,
 * at the boundary, against a mocked axios instance (same pattern as
 * api.duplicateWorkOrder.test.ts). A refusal must reach the caller as a
 * rejection, never a resolved value, so the screen cannot show success for it.
 */

const mockGet = jest.fn();
const mockPost = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: jest.fn() },
    create,
  };
});

import api from './api';

describe('api.changeOwnPassword', () => {
  beforeEach(() => {
    mockPost.mockReset();
  });

  it('posts exactly the current and new password to /users/change-password', async () => {
    mockPost.mockResolvedValue({ data: { message: 'Password changed successfully' } });

    const result = await api.changeOwnPassword({
      current_password: 'old secret phrase 1',
      new_password: 'correct horse battery staple',
    });

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith('/users/change-password', {
      current_password: 'old secret phrase 1',
      new_password: 'correct horse battery staple',
    });
    expect(result).toEqual({ message: 'Password changed successfully' });
  });

  it('rejects on a server refusal rather than resolving', async () => {
    const refusal = Object.assign(new Error('Request failed'), {
      response: { status: 400, data: { detail: 'Current password is incorrect' } },
    });
    mockPost.mockRejectedValue(refusal);

    await expect(
      api.changeOwnPassword({ current_password: 'wrong', new_password: 'correct horse battery staple' })
    ).rejects.toBe(refusal);
  });
});
