/**
 * Multi-carrier shipping API client surface.
 *
 * These tests exercise the PUBLIC ApiService methods added for the shipping
 * integration and assert the URL / verb / payload each sends — the contract the
 * Shipping UX + Admin Carriers tab depend on. axios is mocked at the module
 * boundary (the same "mock the create() instance" pattern as
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

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  mockDelete.mockReset();
  api.clearCache();
});

describe('shipping carrier methods', () => {
  it('validateAddress POSTs to /shipping/validate-address and passes carrier_account_id as a param', async () => {
    mockPost.mockResolvedValueOnce(ok({ is_valid: true, normalized: {}, messages: [] }));
    const payload = { address: { street1: '1 A St', city: 'Tulsa', state: 'OK', zip: '74101' } };

    const result = await api.validateAddress(payload, 7);

    expect(mockPost).toHaveBeenCalledWith('/shipping/validate-address', payload, {
      params: { carrier_account_id: 7 },
    });
    expect(result.is_valid).toBe(true);
  });

  it('validateAddress omits params when no carrier account is given', async () => {
    mockPost.mockResolvedValueOnce(ok({ is_valid: true, normalized: {}, messages: [] }));
    const payload = { address: { street1: '1 A St', city: 'Tulsa', state: 'OK', zip: '74101' } };

    await api.validateAddress(payload);

    expect(mockPost).toHaveBeenCalledWith('/shipping/validate-address', payload, { params: undefined });
  });

  it('rateShop POSTs the parcels/pallets payload to /shipping/{id}/rate-shop and returns the quote list', async () => {
    const quotes = [
      { provider_rate_id: 'r1', carrier: 'FedEx', mode: 'parcel', amount: '12.50', currency: 'USD', is_selected: false },
    ];
    mockPost.mockResolvedValueOnce(ok(quotes));
    const body = { parcels: [{ length_in: 6, width_in: 6, height_in: 6, weight_lbs: 2 }] };

    const result = await api.rateShop(42, body);

    expect(mockPost).toHaveBeenCalledWith('/shipping/42/rate-shop', body);
    expect(result).toEqual(quotes);
  });

  it('getRates GETs the persisted quotes', async () => {
    mockGet.mockResolvedValueOnce(ok([]));
    await api.getRates(42);
    expect(mockGet).toHaveBeenCalledWith('/shipping/42/rates');
  });

  it('buyLabel POSTs { rate_id, carrier_account_id } to /shipping/{id}/buy-label', async () => {
    mockPost.mockResolvedValueOnce(ok({ shipment_id: 42, shipment_number: 'SHP-1', already_purchased: false }));
    const result = await api.buyLabel(42, { rate_id: 'r1', carrier_account_id: 3 });

    expect(mockPost).toHaveBeenCalledWith('/shipping/42/buy-label', { rate_id: 'r1', carrier_account_id: 3 });
    expect(result.already_purchased).toBe(false);
  });

  it('buyBol POSTs to /shipping/{id}/buy-bol', async () => {
    mockPost.mockResolvedValueOnce(ok({ shipment_id: 42, shipment_number: 'SHP-1', already_purchased: false }));
    await api.buyBol(42, { rate_id: 'r1' });
    expect(mockPost).toHaveBeenCalledWith('/shipping/42/buy-bol', { rate_id: 'r1' });
  });

  it('schedulePickup POSTs the window payload to /shipping/{id}/schedule-pickup', async () => {
    mockPost.mockResolvedValueOnce(ok({ provider_pickup_id: 'p1' }));
    const body = { pickup_date: '2026-06-10', window_start: '2026-06-10T09:00:00', window_end: '2026-06-10T17:00:00' };
    await api.schedulePickup(42, body);
    expect(mockPost).toHaveBeenCalledWith('/shipping/42/schedule-pickup', body);
  });

  it('voidLabel and refundLabel POST to their respective endpoints with no body', async () => {
    mockPost.mockResolvedValue(ok({ shipment_id: 42 }));
    await api.voidLabel(42);
    expect(mockPost).toHaveBeenCalledWith('/shipping/42/void-label');
    await api.refundLabel(42);
    expect(mockPost).toHaveBeenCalledWith('/shipping/42/refund');
  });

  it('getTracking GETs /shipping/{id}/tracking', async () => {
    mockGet.mockResolvedValueOnce(ok({ shipment_id: 42, shipment_number: 'SHP-1', events: [] }));
    const result = await api.getTracking(42);
    expect(mockGet).toHaveBeenCalledWith('/shipping/42/tracking');
    expect(result.events).toEqual([]);
  });
});

describe('admin carrier-account + shipping-profile methods', () => {
  it('getCarrierAccounts GETs /admin/settings/carrier-accounts with include_inactive', async () => {
    mockGet.mockResolvedValueOnce(ok([]));
    await api.getCarrierAccounts(true);
    expect(mockGet).toHaveBeenCalledWith('/admin/settings/carrier-accounts', {
      params: { include_inactive: true },
    });
  });

  it('createCarrierAccount POSTs the write-only credential payload', async () => {
    const created = { id: 1, name: 'EasyPost', provider: 'easypost', carrier_refs: [], has_webhook_secret: false };
    mockPost.mockResolvedValueOnce(ok(created));
    const data = { name: 'EasyPost', provider: 'easypost', api_key: 'sk_test_123' };

    const result = await api.createCarrierAccount(data);

    expect(mockPost).toHaveBeenCalledWith('/admin/settings/carrier-accounts', data);
    // The read shape never carries a plaintext key.
    expect(result).not.toHaveProperty('api_key');
  });

  it('updateCarrierAccount PUTs to /admin/settings/carrier-accounts/{id}', async () => {
    mockPut.mockResolvedValueOnce(ok({ id: 1, name: 'Renamed', provider: 'easypost', carrier_refs: [], has_webhook_secret: false }));
    await api.updateCarrierAccount(1, { name: 'Renamed' });
    expect(mockPut).toHaveBeenCalledWith('/admin/settings/carrier-accounts/1', { name: 'Renamed' });
  });

  it('deleteCarrierAccount DELETEs the account (soft delete server-side)', async () => {
    mockDelete.mockResolvedValueOnce(ok({ status: 'ok' }));
    await api.deleteCarrierAccount(5);
    expect(mockDelete).toHaveBeenCalledWith('/admin/settings/carrier-accounts/5');
  });

  it('testCarrierConnection POSTs to the test-connection endpoint', async () => {
    mockPost.mockResolvedValueOnce(ok({ ok: true, provider: 'easypost', message: 'OK' }));
    const result = await api.testCarrierConnection(5);
    expect(mockPost).toHaveBeenCalledWith('/admin/settings/carrier-accounts/5/test-connection');
    expect(result.ok).toBe(true);
  });

  it('getShippingProfile GETs /admin/settings/shipping-profile', async () => {
    mockGet.mockResolvedValueOnce(ok({ id: 1, allow_carrier_egress: false }));
    const result = await api.getShippingProfile();
    expect(mockGet).toHaveBeenCalledWith('/admin/settings/shipping-profile');
    expect(result.allow_carrier_egress).toBe(false);
  });

  it('updateShippingProfile PUTs the profile (incl. the egress kill switch)', async () => {
    mockPut.mockResolvedValueOnce(ok({ id: 1, allow_carrier_egress: true }));
    const result = await api.updateShippingProfile({ allow_carrier_egress: true, ship_from_city: 'Tulsa' });
    expect(mockPut).toHaveBeenCalledWith('/admin/settings/shipping-profile', {
      allow_carrier_egress: true,
      ship_from_city: 'Tulsa',
    });
    expect(result.allow_carrier_egress).toBe(true);
  });
});
