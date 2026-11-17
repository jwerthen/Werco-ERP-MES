/**
 * The custom-report client surface: the URL / verb / payload each typed method sends,
 * the CSV export's RFC 5987 filename, and a blob request's JSON error body parsed back
 * so callers read `detail` as for any other call. axios is mocked at the module
 * boundary (the api.quality.test.ts pattern).
 */
import type { CustomReportResult, ReportTemplate } from '../types/reportBuilder';

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPatch = jest.fn();
const mockDelete = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  patch: mockPatch,
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

const ok = (data: unknown, headers: Record<string, string> = {}) => ({ status: 200, data, headers });

const definition = {
  data_source: 'work_orders',
  columns: [{ field: 'status' }, { field: 'quantity_ordered', aggregate: 'sum' as const, alias: 'total_qty' }],
  filters: [{ field: 'quantity_ordered', operator: 'between' as const, value: 0, value2: 50 }],
  group_by: [{ field: 'status' }],
  sort: [{ field: 'total_qty', direction: 'desc' as const }],
};

const template: ReportTemplate = {
  id: 12,
  name: 'Open work by status',
  description: null,
  ...definition,
  is_shared: true,
  created_by: 3,
  created_at: '2026-11-01T15:00:00Z',
  updated_at: null,
  definition_errors: [],
};

const result: CustomReportResult = {
  columns: [{ key: 'status', label: 'Status', type: 'enum' }],
  rows: [{ status: 'released' }],
  row_count: 1,
  truncated: false,
  notes: [],
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
  api.clearCache();
});

describe('report catalog and templates', () => {
  it('reads the catalog and the template list', async () => {
    mockGet.mockResolvedValueOnce(ok({ work_orders: { label: 'Work Orders', fields: [] } }));
    mockGet.mockResolvedValueOnce(ok([template]));
    await expect(api.getDataSources()).resolves.toEqual({ work_orders: { label: 'Work Orders', fields: [] } });
    await expect(api.listReportTemplates()).resolves.toEqual([template]);
    expect(mockGet).toHaveBeenNthCalledWith(1, '/analytics/data-sources');
    expect(mockGet).toHaveBeenNthCalledWith(2, '/analytics/custom-report/templates');
  });

  it('get / create / patch / delete hit the template routes', async () => {
    mockGet.mockResolvedValueOnce(ok(template));
    mockPost.mockResolvedValueOnce(ok(template));
    mockPatch.mockResolvedValueOnce(ok({ ...template, name: 'Renamed' }));
    mockDelete.mockResolvedValueOnce(ok({ message: 'Report template deleted' }));

    await api.getReportTemplate(12);
    await api.createReportTemplate({ name: 'Open work by status', is_shared: true, ...definition });
    const updated = await api.updateReportTemplate(12, { name: 'Renamed', definition });
    const deleted = await api.deleteReportTemplate(12);

    expect(mockGet).toHaveBeenCalledWith('/analytics/custom-report/templates/12');
    expect(mockPost).toHaveBeenCalledWith('/analytics/custom-report/templates', {
      name: 'Open work by status',
      is_shared: true,
      ...definition,
    });
    expect(mockPatch).toHaveBeenCalledWith('/analytics/custom-report/templates/12', { name: 'Renamed', definition });
    expect(updated.name).toBe('Renamed');
    expect(mockDelete).toHaveBeenCalledWith('/analytics/custom-report/templates/12');
    expect(deleted).toEqual({ message: 'Report template deleted' });
  });
});

describe('running reports', () => {
  it('runCustomReport POSTs the typed request', async () => {
    mockPost.mockResolvedValueOnce(ok(result));
    await expect(api.runCustomReport({ ...definition, limit: 250 })).resolves.toEqual(result);
    expect(mockPost).toHaveBeenCalledWith('/analytics/custom-report', { ...definition, limit: 250 });
  });

  it('runReportTemplate POSTs to the run route, with limit only when given', async () => {
    mockPost.mockResolvedValue(ok(result));
    await api.runReportTemplate(12);
    expect(mockPost).toHaveBeenLastCalledWith('/analytics/custom-report/templates/12/run', undefined, undefined);
    await api.runReportTemplate(12, 5000);
    expect(mockPost).toHaveBeenLastCalledWith('/analytics/custom-report/templates/12/run', undefined, {
      params: { limit: 5000 },
    });
  });
});

describe('exportReportTemplateCsv', () => {
  it('requests a CSV blob and decodes the RFC 5987 filename', async () => {
    const blob = new Blob(['status\nreleased\n'], { type: 'text/csv' });
    mockGet.mockResolvedValueOnce(
      ok(blob, {
        'content-disposition':
          'attachment; filename="Ruckstand_by_status.csv"; filename*=UTF-8\'\'R%C3%BCckstand%20%22by%22%20status.csv',
      })
    );
    const exported = await api.exportReportTemplateCsv(12);
    expect(mockGet).toHaveBeenCalledWith('/analytics/custom-report/export', {
      params: { template_id: 12, format: 'csv' },
      responseType: 'blob',
    });
    expect(exported.blob).toBe(blob);
    expect(exported.filename).toBe('Rückstand "by" status.csv');
  });

  it('falls back to the ASCII filename, then to a default', async () => {
    mockGet.mockResolvedValueOnce(ok(new Blob(['x']), { 'content-disposition': 'attachment; filename="plain.csv"' }));
    await expect(api.exportReportTemplateCsv(12)).resolves.toMatchObject({ filename: 'plain.csv' });
    mockGet.mockResolvedValueOnce(ok(new Blob(['x'])));
    await expect(api.exportReportTemplateCsv(12)).resolves.toMatchObject({ filename: 'custom_report.csv' });
  });

  it('parses a JSON error body out of the blob so detail reads normally', async () => {
    const error = Object.assign(new Error('Request failed with status code 400'), {
      response: {
        status: 400,
        data: new Blob([JSON.stringify({ detail: 'Too many rows to export (over 10,000).' })], {
          type: 'application/json',
        }),
      },
    });
    mockGet.mockRejectedValueOnce(error);
    await expect(api.exportReportTemplateCsv(12)).rejects.toBe(error);
    expect(error.response.data).toEqual({ detail: 'Too many rows to export (over 10,000).' });
  });
});
