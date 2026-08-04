/**
 * SPC client-boundary contract.
 *
 * The page-level suite (`pages/SPC.cockpit.test.tsx`) mocks this module, so the two
 * behaviours that live inside the client itself have no coverage there:
 *
 *  1. `getSPCCharacteristics` pages the route to completion. GET /spc/characteristics
 *     returns a BARE ARRAY with no envelope and no total, and defaults to `limit=100`
 *     (max 500) — so an unpaged call both drops everything past the 100th characteristic
 *     by name AND lets the caller render that 100 as if it were the whole set.
 *  2. Every SPC method returns `response.data` — the unwrapped payload. Re-unwrapping
 *     that with `.data` is what made the SPC page's characteristic list permanently
 *     empty in production, so the shape is pinned here at its source.
 *
 * axios is mocked at the module boundary (same pattern as api.laserNest.test.ts).
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
import type { SPCCharacteristic } from '../types/spc';

const ok = (data: unknown) => ({ status: 200, data, headers: {} });

const characteristic = (id: number): SPCCharacteristic => ({
  id,
  name: `Characteristic ${id}`,
  part_id: 1,
  characteristic_type: 'dimensional',
  unit_of_measure: 'in',
  specification_nominal: 1,
  specification_usl: 1.1,
  specification_lsl: 0.9,
  chart_type: 'xbar_r',
  subgroup_size: 5,
  work_center_id: null,
  operation_number: null,
  is_active: true,
  is_critical: false,
  notes: null,
  created_at: '2026-06-01T12:00:00',
  updated_at: null,
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
  mockDelete.mockReset();
  api.clearCache();
});

describe('getSPCCharacteristics', () => {
  it('returns the bare array unwrapped, with no envelope', async () => {
    mockGet.mockResolvedValueOnce(ok([characteristic(1), characteristic(2)]));

    const result = await api.getSPCCharacteristics({ is_active: true });

    expect(Array.isArray(result)).toBe(true);
    expect(result.map((c) => c.id)).toEqual([1, 2]);
  });

  it('pages past the route default instead of silently truncating at 100', async () => {
    const firstPage = Array.from({ length: 500 }, (_, i) => characteristic(i + 1));
    const secondPage = Array.from({ length: 12 }, (_, i) => characteristic(i + 501));
    mockGet.mockResolvedValueOnce(ok(firstPage)).mockResolvedValueOnce(ok(secondPage));

    const result = await api.getSPCCharacteristics({ is_active: true });

    expect(result).toHaveLength(512);
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(mockGet).toHaveBeenNthCalledWith(1, '/spc/characteristics', {
      params: { is_active: true, skip: 0, limit: 500 },
    });
    expect(mockGet).toHaveBeenNthCalledWith(2, '/spc/characteristics', {
      params: { is_active: true, skip: 500, limit: 500 },
    });
  });

  it('stops paging on a short page', async () => {
    mockGet.mockResolvedValueOnce(ok([characteristic(1)]));

    await api.getSPCCharacteristics();

    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it('honours an explicit limit as a single request', async () => {
    // An explicit `limit` is a caller asking for a bounded slice — do not page over it.
    mockGet.mockResolvedValueOnce(ok(Array.from({ length: 25 }, (_, i) => characteristic(i + 1))));

    await api.getSPCCharacteristics({ limit: 25 });

    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith('/spc/characteristics', { params: { limit: 25 } });
  });
});

describe('getSPCMeasurements', () => {
  it('passes start_date through so the window can be bounded by time', async () => {
    // The route orders ASC and THEN limits, so `limit` alone can only ever return the
    // OLDEST rows. `start_date` is the only parameter that reaches recent measurements.
    mockGet.mockResolvedValueOnce(ok([]));

    await api.getSPCMeasurements(7, { start_date: '2026-06-01T08:25:00' });

    expect(mockGet).toHaveBeenCalledWith('/spc/measurements/7', {
      params: { start_date: '2026-06-01T08:25:00' },
    });
  });
});
