/**
 * Company-creation client boundary.
 *
 * Werco is the only company on this system (owner decision 2026-11-23), so the
 * unauthenticated `POST /companies/register` self-registration route, its page
 * and its client method were deleted. The one company-creation call the SPA keeps
 * is the platform-admin `createCompany`, which must post to `/platform/companies`
 * and nowhere else.
 *
 * axios is mocked at the module boundary (same pattern as api.duplicateWorkOrder.test.ts).
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

describe('company creation client', () => {
  beforeEach(() => {
    mockPost.mockReset();
  });

  it('no longer exposes the deleted public self-registration call', () => {
    expect((api as unknown as Record<string, unknown>).registerCompany).toBeUndefined();
  });

  it('creates a company only through the platform-admin route', async () => {
    mockPost.mockResolvedValue({ status: 200, data: { id: 2, name: 'Example Fab Co' }, headers: {} });

    const result = await api.createCompany({ name: 'Example Fab Co' });

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith('/platform/companies', { name: 'Example Fab Co' });
    expect(result).toEqual({ id: 2, name: 'Example Fab Co' });
  });
});
