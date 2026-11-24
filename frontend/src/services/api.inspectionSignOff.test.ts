/**
 * Inspection sign-off client boundary.
 *
 * The page and component suites mock this module, so they cannot see which URL the
 * real method posts to or what body it sends. This file pins both:
 *   - `signOffOperationInspection` POSTs to `/shop-floor/operations/{id}/inspection`
 *     (the audited verb, the ONLY writer of `inspection_complete`) with the body the
 *     caller passed, `{}` by default, and hands the typed response back intact;
 *   - `updateOperation` PUTs the generic edit, and its payload type has no
 *     `inspection_complete`, so a UI form that tries to set the flag through the PUT
 *     (refused 422 by the server) is a compile error under `npm run type-check`.
 *
 * axios is mocked at the module boundary (same pattern as the sibling api.*.test.ts files).
 */

const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  put: mockPut,
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
import type { OperationInspectionResponse, WorkOrderOperationUpdatePayload } from '../types';

const signed: OperationInspectionResponse = {
  message: 'Operation inspection recorded',
  operation_id: 71,
  work_order_id: 42,
  requires_inspection: true,
  inspection_complete: true,
  inspection_type: 'final',
  signed_off_by_id: 9,
  signed_off_by_name: 'Dana Quill',
  signed_off_at: '2026-11-23T15:04:05.123456Z',
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe('signOffOperationInspection', () => {
  it('posts the type and notes to the audited sign-off verb and returns its response', async () => {
    mockPost.mockResolvedValue({ status: 200, data: signed, headers: {} });

    const result = await api.signOffOperationInspection(71, { inspection_type: 'final', notes: 'bore in tolerance' });

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith('/shop-floor/operations/71/inspection', {
      inspection_type: 'final',
      notes: 'bore in tolerance',
    });
    expect(result).toEqual(signed);
    expect(mockPut).not.toHaveBeenCalled();
  });

  it('sends an empty body when called with no fields', async () => {
    mockPost.mockResolvedValue({ status: 200, data: signed, headers: {} });

    await api.signOffOperationInspection(71);

    expect(mockPost).toHaveBeenCalledWith('/shop-floor/operations/71/inspection', {});
  });

  it('lets a refusal reach the caller', async () => {
    const refusal = { response: { status: 403, data: { detail: 'not an API token' } } };
    mockPost.mockRejectedValue(refusal);

    await expect(api.signOffOperationInspection(71, {})).rejects.toBe(refusal);
  });
});

describe('updateOperation', () => {
  it('puts the generic edit and never carries inspection_complete', async () => {
    mockPut.mockResolvedValue({ status: 200, data: {}, headers: {} });
    const payload: WorkOrderOperationUpdatePayload = { version: 3, requires_inspection: true };

    await api.updateOperation(72, payload);

    expect(mockPut).toHaveBeenCalledWith('/work-orders/operations/72', { version: 3, requires_inspection: true });
    expect(mockPut.mock.calls[0][1]).not.toHaveProperty('inspection_complete');
  });

  it('refuses inspection_complete at compile time', () => {
    const bad: WorkOrderOperationUpdatePayload = {
      version: 1,
      // @ts-expect-error inspection_complete is written only by the audited sign-off verb
      inspection_complete: true,
    };
    expect(bad.version).toBe(1);
  });
});
