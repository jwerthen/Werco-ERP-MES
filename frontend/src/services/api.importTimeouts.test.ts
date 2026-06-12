/**
 * Import Center request timeouts.
 *
 * Every Import Center upload call must carry an explicit axios `timeout` so a
 * slow/stuck parse fails fast with ECONNABORTED (which ImportCenter.tsx maps to
 * a friendly message) instead of hanging the browser tab indefinitely. The
 * timeout is phase-dependent: dry-run validation is a bounded server-side parse
 * (120s), while commits write per-row (audit logging, bcrypt for users) and may
 * legitimately run long on max-size files (10 min) — aborting a commit early
 * would misreport an import the server is still processing. Without the timeout
 * config, the ECONNABORTED handling in ImportCenter is dead code — these tests
 * pin the config on every import method. axios is mocked at the module boundary
 * (the same "mock the create() instance" pattern as api.shipping.test.ts /
 * api.dashboardCache.test.ts).
 */

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
const mockDelete = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  put: mockPut,
  delete: mockDelete,
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

const ok = (data: unknown) => ({ status: 200, data, headers: {} });
const csvFile = new File(['part_number\nP-1\n'], 'import.csv', { type: 'text/csv' });

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  api.clearCache();
});

describe('Import Center upload timeouts', () => {
  const uploadCases: Array<[string, (dryRun: boolean) => Promise<unknown>, string]> = [
    ['importPartsCsv', (dryRun) => api.importPartsCsv(csvFile, dryRun), '/parts/import-csv'],
    ['importMaterialsCsv', (dryRun) => api.importMaterialsCsv(csvFile, dryRun), '/materials/import-csv'],
    ['importCustomersCsv', (dryRun) => api.importCustomersCsv(csvFile, dryRun), '/customers/import-csv'],
    ['importWorkCentersCsv', (dryRun) => api.importWorkCentersCsv(csvFile, dryRun), '/work-centers/import-csv'],
    ['importVendorsCsv', (dryRun) => api.importVendorsCsv(csvFile, dryRun), '/purchasing/vendors/import-csv'],
    ['importUsersCsv', (dryRun) => api.importUsersCsv(csvFile, undefined, dryRun), '/users/import-csv'],
    ['importWorkOrders', (dryRun) => api.importWorkOrders(csvFile, dryRun), '/work-orders/import'],
    [
      'importPurchaseOrders',
      (dryRun) => api.importPurchaseOrders(csvFile, dryRun),
      '/purchasing/purchase-orders/import',
    ],
  ];

  it.each(uploadCases)('%s sends timeout: 120000 on dry-run validation', async (_name, call, url) => {
    mockPost.mockResolvedValueOnce(ok({ imported_count: 0, errors: [] }));

    await call(true);

    expect(mockPost).toHaveBeenCalledTimes(1);
    const [calledUrl, body, config] = mockPost.mock.calls[0];
    expect(calledUrl).toBe(url);
    expect(body).toBeInstanceOf(FormData);
    expect(config).toMatchObject({ timeout: 120000, params: { dry_run: true } });
  });

  it.each(uploadCases)('%s sends timeout: 600000 on commit', async (_name, call, url) => {
    mockPost.mockResolvedValueOnce(ok({ imported_count: 0, errors: [] }));

    await call(false);

    expect(mockPost).toHaveBeenCalledTimes(1);
    const [calledUrl, body, config] = mockPost.mock.calls[0];
    expect(calledUrl).toBe(url);
    expect(body).toBeInstanceOf(FormData);
    expect(config).toMatchObject({ timeout: 600000, params: { dry_run: false } });
  });

  it('downloadImportTemplate sends a 30s timeout and requests a blob', async () => {
    mockGet.mockResolvedValueOnce({
      status: 200,
      data: new Blob(['xlsx-bytes']),
      headers: { 'content-disposition': 'attachment; filename="parts_template.xlsx"' },
    });

    const result = await api.downloadImportTemplate('parts');

    expect(mockGet).toHaveBeenCalledWith('/import/templates/parts', {
      responseType: 'blob',
      timeout: 30000,
    });
    expect(result.filename).toBe('parts_template.xlsx');
  });
});
