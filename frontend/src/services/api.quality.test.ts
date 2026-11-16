/**
 * The quality-record API client surface added with the CAR form, the printable CAR
 * and the FAI log of record: the URL / verb / payload each method sends. axios is
 * mocked at the module boundary (the api.laserNest.test.ts pattern).
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

const ok = (data: unknown, headers: Record<string, string> = {}) => ({ status: 200, data, headers });

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  mockDelete.mockReset();
  api.clearCache();
});

describe('CAR', () => {
  it('approveCARQuality POSTs with no body — the server stamps the approver', async () => {
    mockPost.mockResolvedValueOnce(ok({ id: 9, quality_approved_by: 3 }));
    const result = await api.approveCARQuality(9);
    expect(mockPost).toHaveBeenCalledWith('/quality/car/9/quality-approval');
    expect(result.quality_approved_by).toBe(3);
  });

  it('fetchCARPdf requests the PDF as a blob and returns an object URL', async () => {
    mockGet.mockResolvedValueOnce(ok(new Blob(['%PDF-1.4'])));
    const url = await api.fetchCARPdf(9);
    expect(mockGet).toHaveBeenCalledWith('/quality/car/9/pdf', { responseType: 'blob' });
    expect(typeof url).toBe('string');
  });
});

describe('FAI', () => {
  it('recordFAICustomerApproval POSTs the optional date', async () => {
    mockPost.mockResolvedValue(ok({ id: 4, customer_approved: true }));
    await api.recordFAICustomerApproval(4, { customer_approval_date: '2026-11-10' });
    expect(mockPost).toHaveBeenLastCalledWith('/quality/fai/4/customer-approval', {
      customer_approval_date: '2026-11-10',
    });
    await api.recordFAICustomerApproval(4);
    expect(mockPost).toHaveBeenLastCalledWith('/quality/fai/4/customer-approval', {});
  });

  it('characteristic add / record / remove hit the nested routes', async () => {
    mockPost.mockResolvedValueOnce(ok({ id: 71 }));
    mockPut.mockResolvedValueOnce(ok({ id: 71 }));
    mockDelete.mockResolvedValueOnce(ok({ message: 'Characteristic deleted' }));

    await api.addFAICharacteristic(4, { char_number: 1, characteristic: 'Bore diameter' });
    await api.updateFAICharacteristic(4, 71, { actual_value: '0.5001', is_conforming: true });
    await api.deleteFAICharacteristic(4, 71);

    expect(mockPost).toHaveBeenCalledWith('/quality/fai/4/characteristics', {
      char_number: 1,
      characteristic: 'Bore diameter',
    });
    expect(mockPut).toHaveBeenCalledWith('/quality/fai/4/characteristics/71', {
      actual_value: '0.5001',
      is_conforming: true,
    });
    expect(mockDelete).toHaveBeenCalledWith('/quality/fai/4/characteristics/71');
  });

  it('getFAILog sends the filters and paging as query params', async () => {
    mockGet.mockResolvedValueOnce(ok([]));
    await api.getFAILog({ status: 'passed', start_date: '2026-01-01', limit: 51, offset: 50 });
    expect(mockGet).toHaveBeenCalledWith('/quality/fai/log', {
      params: { status: 'passed', start_date: '2026-01-01', limit: 51, offset: 50 },
    });
  });

  it('exportFAILog defaults to xlsx and names the file from Content-Disposition', async () => {
    const blob = new Blob(['x']);
    mockGet.mockResolvedValueOnce(
      ok(blob, { 'content-disposition': 'attachment; filename=fai_log_20261113_101500.xlsx' })
    );
    const result = await api.exportFAILog({ customer: 'Acme' });
    expect(mockGet).toHaveBeenCalledWith('/exports/fai/export', {
      params: { customer: 'Acme', format: 'xlsx' },
      responseType: 'blob',
    });
    expect(result).toEqual({ blob, filename: 'fai_log_20261113_101500.xlsx' });
  });
});
