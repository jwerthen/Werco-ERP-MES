/**
 * Clear-inspection client boundary — the ROUTE and the response contract.
 *
 * `POST /receiving/receipt/{id}/clear-inspection` is the non-destructive exit
 * from the inspection queue, and it sits one character away from its destructive
 * neighbour: `POST /receiving/receipt/{id}/void` un-receives the material
 * entirely and forces the whole receipt to be re-keyed. Both take `{ reason }`.
 * A mistake in the path or the verb here does not fail loudly — it silently
 * routes a "the box was ticked by mistake" click into a void.
 *
 * This file exists because the page-level suite CANNOT catch that: it mocks this
 * module, so a mock that honours the declared return type stays green no matter
 * which URL the real method posts to (the standing argument in
 * `api.duplicateWorkOrder.test.ts` and `api.spc.test.ts`).
 *
 * It also pins the response shape reaching the caller intact. Those five fields
 * are how the verb's outcome is checkable, and two of them are compliance
 * claims, not conveniences:
 *   - `inspection_status` is `not_required`, NEVER `passed` — no incoming
 *     inspection happened, so the record must not assert one did (the AS9100D
 *     records-integrity rule flagged on PR #127);
 *   - `requires_inspection` has flipped false — that flag is the ONLY record
 *     that stock was placed, and the correct/void reconciler reads it as
 *     `inventory_placed`.
 *
 * axios is mocked at the module boundary (same pattern as the sibling files).
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

/** Exactly what the endpoint serializes on success (a ReceiptResponse). */
const clearedReceipt = (overrides: Record<string, unknown> = {}) => ({
  id: 42,
  receipt_number: 'RCV-20260618-001',
  po_line_id: 51,
  quantity_received: 5,
  quantity_accepted: 5,
  quantity_rejected: 0,
  lot_number: 'LOT-9',
  status: 'accepted',
  inspection_status: 'not_required',
  requires_inspection: false,
  inspection_method: null,
  inspected_by: null,
  inspected_at: null,
  received_at: '2026-06-18T12:00:00Z',
  ...overrides,
});

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

describe('api.clearReceiptInspection: request', () => {
  it('POSTs to the clear-inspection route — not the adjacent void route', async () => {
    mockPost.mockResolvedValue(ok(clearedReceipt()));

    await api.clearReceiptInspection(42, { reason: 'Box ticked by mistake.' });

    expect(mockPost).toHaveBeenCalledWith('/receiving/receipt/42/clear-inspection', {
      reason: 'Box ticked by mistake.',
    });
    // The regression guard: /void is the destructive neighbour that would
    // un-receive the material and force a re-key.
    expect(mockPost).not.toHaveBeenCalledWith(
      '/receiving/receipt/42/void',
      expect.anything(),
    );
  });

  it('sends the reason verbatim as the whole body', async () => {
    // The reason lands on the tamper-evident audit trail, so the client must not
    // reshape, re-key, or decorate it.
    mockPost.mockResolvedValue(ok(clearedReceipt()));

    await api.clearReceiptInspection(7, {
      reason: 'Stock hardware — no incoming inspection required.',
    });

    expect(mockPost).toHaveBeenCalledWith('/receiving/receipt/7/clear-inspection', {
      reason: 'Stock hardware — no incoming inspection required.',
    });
  });
});

describe('api.clearReceiptInspection: the receipt reaches the caller intact', () => {
  it('returns the receipt itself, not an envelope', async () => {
    mockPost.mockResolvedValue(ok(clearedReceipt()));

    const result = await api.clearReceiptInspection(42, { reason: 'Box ticked by mistake.' });

    expect(result.id).toBe(42);
    expect(result.receipt_number).toBe('RCV-20260618-001');
    expect(result).not.toHaveProperty('receipt');
  });

  it('carries the compliance-bearing fields through unchanged', async () => {
    mockPost.mockResolvedValue(ok(clearedReceipt()));

    const result = await api.clearReceiptInspection(42, { reason: 'Box ticked by mistake.' });

    expect(result.status).toBe('accepted');
    // NOT_REQUIRED, never PASSED — no inspection happened (PR #127).
    expect(result.inspection_status).toBe('not_required');
    expect(result.inspection_status).not.toBe('passed');
    // The inventory_placed discriminator the correct/void reconciler reads.
    expect(result.requires_inspection).toBe(false);
    // No inspection was performed, so no inspector is stamped.
    expect(result.inspection_method).toBeNull();
    expect(result.inspected_by).toBeNull();
    expect(result.inspected_at).toBeNull();
  });
});
