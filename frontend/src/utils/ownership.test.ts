/**
 * `ownershipChangeNotice` — the toast after a SUCCESSFUL change of ownership.
 *
 * The server never refuses on these disclosures; it reports them (and writes them to the
 * audit row). The toast is where the person learns that open jobs now depend on someone
 * else's material or are short of company stock, so either case must be a `warning`
 * naming the work orders, never a plain success that hides it.
 */
import type { InventoryOwnershipChangeResult } from '../types/ownership';
import { ownershipChangeNotice } from './ownership';

const result = (overrides: Partial<InventoryOwnershipChangeResult> = {}): InventoryOwnershipChangeResult => ({
  id: 31,
  part_id: 7,
  location: 'RAW-A',
  warehouse: 'MAIN',
  quantity_on_hand: 8,
  quantity_allocated: 0,
  quantity_available: 8,
  lot_number: 'HEAT-31',
  serial_number: null,
  status: 'available',
  is_active: true,
  received_date: '2026-11-02T15:04:05Z',
  expiration_date: null,
  created_at: '2026-11-02T15:04:05Z',
  updated_at: '2026-11-19T16:00:00Z',
  ownership: 'customer',
  owner_customer_id: 5,
  owner_vendor_id: null,
  owner_name: 'Invented Rotor Labs',
  open_pinned_ties: [],
  unpinned_demand: null,
  ...overrides,
});

const tie = (allocationId: number, workOrderNumber: string | null, workOrderId = allocationId + 100) => ({
  allocation_id: allocationId,
  work_order_id: workOrderId,
  work_order_number: workOrderNumber,
  outstanding_quantity: 2,
});

it('is a plain success when neither disclosure needs action', () => {
  expect(ownershipChangeNotice(result())).toEqual({ type: 'success', message: 'Ownership changed.' });
});

it('is a plain success when the remaining company stock still covers the open unpinned demand', () => {
  const notice = ownershipChangeNotice(
    result({
      unpinned_demand: {
        open_unpinned_demand: 6,
        company_stock_remaining: 10,
        short_by: 0,
        work_order_numbers: ['WO-INV-1'],
      },
    })
  );
  expect(notice).toEqual({ type: 'success', message: 'Ownership changed.' });
});

it('warns, naming each pinned job once, when open jobs are pinned to now customer-owned stock', () => {
  const notice = ownershipChangeNotice(
    result({ open_pinned_ties: [tie(1, 'WO-INV-1'), tie(2, 'WO-INV-1'), tie(3, 'WO-INV-2')] })
  );
  expect(notice).toEqual({
    type: 'warning',
    message:
      'Ownership changed. 2 open jobs are pinned to this lot (WO-INV-1, WO-INV-2) and will still consume it at completion.',
  });
});

it('does not warn about pins on a row returned to the company — those draw company stock as normal', () => {
  const notice = ownershipChangeNotice(
    result({ ownership: 'company', owner_customer_id: null, owner_name: null, open_pinned_ties: [tie(1, 'WO-INV-1')] })
  );
  expect(notice).toEqual({ type: 'success', message: 'Ownership changed.' });
});

it('warns with the shortfall and its work orders when company stock is re-owned away', () => {
  const notice = ownershipChangeNotice(
    result({
      ownership: 'supplier',
      owner_customer_id: null,
      owner_vendor_id: 8,
      unpinned_demand: {
        open_unpinned_demand: 6,
        company_stock_remaining: 2,
        short_by: 4,
        work_order_numbers: ['WO-INV-7', 'WO-INV-8'],
      },
    })
  );
  expect(notice).toEqual({
    type: 'warning',
    message: 'Ownership changed. Company stock is now short by 4 for WO-INV-7, WO-INV-8.',
  });
});

it('reports both disclosures in one warning', () => {
  const notice = ownershipChangeNotice(
    result({
      open_pinned_ties: [tie(1, 'WO-INV-1')],
      unpinned_demand: {
        open_unpinned_demand: 5,
        company_stock_remaining: 1.5,
        short_by: 3.5,
        work_order_numbers: ['WO-INV-9'],
      },
    })
  );
  expect(notice.type).toBe('warning');
  expect(notice.message).toBe(
    'Ownership changed. 1 open job is pinned to this lot (WO-INV-1) and will still consume it at completion; ' +
      'company stock is now short by 3.5 for WO-INV-9.'
  );
});

it('falls back to the work order id, says "this stock" without a lot, and caps a long list', () => {
  const ties = [tie(1, null, 41), ...[2, 3, 4, 5, 6, 7].map(id => tie(id, `WO-INV-${id}`))];
  const notice = ownershipChangeNotice(result({ lot_number: null, open_pinned_ties: ties }));
  expect(notice.type).toBe('warning');
  expect(notice.message).toBe(
    'Ownership changed. 7 open jobs are pinned to this stock (work order #41, WO-INV-2, WO-INV-3, WO-INV-4, ' +
      'WO-INV-5 and 2 more) and will still consume it at completion.'
  );
});
