/**
 * Company-creation client boundary.
 *
 * Werco is the only company on this system (owner decisions 2026-11-23), so both
 * company-creation doors are gone: the unauthenticated `POST /companies/register`
 * self-registration route and the platform-admin `POST /platform/companies` route,
 * along with their pages, modal and client methods. The only way a company is
 * created is the server-side empty-install bootstrap, which the SPA never calls
 * as "create a company". This file pins that the client exposes no creation call.
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
  it('no longer exposes the deleted public self-registration call', () => {
    expect((api as unknown as Record<string, unknown>).registerCompany).toBeUndefined();
  });

  it('no longer exposes the deleted platform-admin company creation call', () => {
    expect((api as unknown as Record<string, unknown>).createCompany).toBeUndefined();
  });

  it('still lists platform companies with a GET, never a POST', async () => {
    mockGet.mockResolvedValue({ status: 200, data: [{ id: 1, name: 'Example Fab Co' }], headers: {} });

    const result = await api.getPlatformCompanies();

    expect(mockGet).toHaveBeenCalledWith('/platform/companies');
    expect(mockPost).not.toHaveBeenCalled();
    expect(result).toEqual([{ id: 1, name: 'Example Fab Co' }]);
  });
});
