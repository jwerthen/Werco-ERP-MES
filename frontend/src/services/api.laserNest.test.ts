/**
 * Manual laser-nest + per-nest PDF API client surface.
 *
 * Asserts the URL / verb / payload each new ApiService method sends (the
 * contract the WorkOrderDetail nest controls and the operator inline-preview
 * depend on), and that fetchLaserNestDocument turns the blob response into an
 * object URL. axios is mocked at the module boundary (same pattern as
 * api.shipping.test.ts).
 */

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

const ok = (data: unknown) => ({ status: 200, data, headers: {} });

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
  api.clearCache();
});

describe('manual laser-nest api methods', () => {
  it('createManualLaserNest POSTs under the parent work order', async () => {
    mockPost.mockResolvedValueOnce(ok({ id: 5, nest_name: '8001', planned_runs: 3 }));
    const body = { cnc_number: '8001', planned_runs: 3, material: '304 SS' };

    const result = await api.createManualLaserNest(42, body);

    expect(mockPost).toHaveBeenCalledWith('/work-orders/42/laser-nests/manual', body);
    expect(result.id).toBe(5);
  });

  it('updateLaserNest PATCHes the per-nest route', async () => {
    mockPatch.mockResolvedValueOnce(ok({ id: 5, nest_name: '8001', planned_runs: 9 }));

    await api.updateLaserNest(5, { planned_runs: 9 });

    expect(mockPatch).toHaveBeenCalledWith('/laser-nests/5', { planned_runs: 9 });
  });

  it('attachLaserNestDocument POSTs the document id', async () => {
    mockPost.mockResolvedValueOnce(ok({ id: 5, has_document: true }));

    await api.attachLaserNestDocument(5, 777);

    expect(mockPost).toHaveBeenCalledWith('/laser-nests/5/attach-document', { document_id: 777 });
  });

  it('detachLaserNestDocument DELETEs the nest document', async () => {
    mockDelete.mockResolvedValueOnce(ok({ id: 5, has_document: false }));

    await api.detachLaserNestDocument(5);

    expect(mockDelete).toHaveBeenCalledWith('/laser-nests/5/document');
  });

  it('deleteLaserNest soft-deletes via the per-nest DELETE endpoint', async () => {
    mockDelete.mockResolvedValueOnce(ok({ message: 'Laser nest deleted', id: 5 }));

    const result = await api.deleteLaserNest(5);

    expect(mockDelete).toHaveBeenCalledWith('/laser-nests/5');
    expect(result).toEqual({ message: 'Laser nest deleted', id: 5 });
  });

  it('fetchLaserNestDocument GETs the inline PDF as a blob and returns an object URL', async () => {
    const blobBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46]); // %PDF
    mockGet.mockResolvedValueOnce(ok(blobBytes));
    const createSpy = jest
      .spyOn(window.URL, 'createObjectURL')
      .mockReturnValue('blob:laser-nest-5');

    const url = await api.fetchLaserNestDocument(5);

    expect(mockGet).toHaveBeenCalledWith('/laser-nests/5/document', { responseType: 'blob' });
    expect(url).toBe('blob:laser-nest-5');
    expect(createSpy).toHaveBeenCalledTimes(1);
    createSpy.mockRestore();
  });
});
