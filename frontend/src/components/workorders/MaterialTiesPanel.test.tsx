/**
 * Material Ties panel.
 *
 * The assertions here are the compliance-shaped ones, not the cosmetic ones:
 *  - an UNTIED work order shows the empty state and never nags;
 *  - CANCELLED ties stay listed (they are the tombstones the inventory ledger's
 *    `allocation_id` resolves to) rather than being tidied away;
 *  - an `open` tie is NOT painted red — statusColors maps `open` to the
 *    defect/NCR red, which would make every healthy tie look cancelled;
 *  - "fully consumed" is derived from `qty_consumed >= qty_planned`, never from
 *    status (`closed` is reserved and never written);
 *  - a DETACHED tie says so, instead of reading as one that was always
 *    work-order-scoped;
 *  - `qty_consumed` is labelled as a reported cache, not a compliance figure;
 *  - edits/unties are server-GATED, so they are NON-optimistic: the row does not
 *    change until the server confirms, refusals render verbatim, and an object
 *    `detail` never reaches the DOM as "[object Object]";
 *  - the panel is gated on work_orders:edit.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MaterialTiesPanel from './MaterialTiesPanel';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { MaterialAllocation } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getMaterialAllocations: jest.fn(),
    updateMaterialAllocation: jest.fn(),
    deleteMaterialAllocation: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const makeTie = (overrides: Partial<MaterialAllocation> = {}): MaterialAllocation => ({
  id: 1,
  work_order_id: 42,
  work_order_operation_id: 71,
  operation_number: '10',
  detached_from_operation_id: null,
  part_id: 55,
  part_number: 'SHT-.125-304',
  part_name: '.125 304 sheet',
  source: 'nest',
  status: 'open',
  qty_per_run: 1,
  qty_planned: 3,
  unit_of_measure: 'EA',
  qty_consumed: 0,
  pinned_inventory_item_id: null,
  pinned_lot_number: null,
  notes: null,
  created_by: 1,
  created_at: '2026-07-24T14:05:00Z',
  updated_at: '2026-07-24T14:05:00Z',
  ...overrides,
});

const renderPanel = (canEdit = true) =>
  render(
    <ToastProvider>
      <MaterialTiesPanel workOrderId={42} workOrderUpdatedAt="2026-07-24T14:05:00Z" canEdit={canEdit} />
    </ToastProvider>
  );

const rowFor = (partNumber: string) => screen.getByText(partNumber).closest('tr') as HTMLElement;

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getMaterialAllocations.mockResolvedValue([makeTie()]);
});

describe('MaterialTiesPanel — reading', () => {
  it('asks for INACTIVE rows too — cancelled ties are the ledger tombstones', async () => {
    renderPanel();
    await screen.findByText('SHT-.125-304');
    // Default arg is include_inactive = true; the panel must not narrow it.
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledWith(42);
  });

  it('says an untied work order behaves exactly as it did before, and never nags', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([]);
    renderPanel();

    expect(await screen.findByText('No material tied to this work order')).toBeInTheDocument();
    expect(screen.getByText(/nothing is deducted from stock/i)).toBeInTheDocument();
  });

  it('treats a non-array response as "no ties" instead of throwing', async () => {
    // Load-bearing beyond this file: all six WorkOrderDetail suites hand-write
    // their api mock as a bare `getMaterialAllocations: jest.fn()`, which resolves
    // to `undefined`. Without the Array.isArray guard the panel would throw inside
    // the page's own render and take those suites down with it — so the guard is
    // pinned here rather than left as an implementation detail.
    mockApi.getMaterialAllocations.mockResolvedValue(undefined as never);
    renderPanel();

    expect(await screen.findByText('No material tied to this work order')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('states that deduction happens at WORK-ORDER completion, not per operation', async () => {
    renderPanel();
    await screen.findByText('SHT-.125-304');
    expect(screen.getByText(/not as each operation completes/i)).toBeInTheDocument();
  });

  it('keeps a cancelled tie listed and dims it rather than filtering it out', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie(),
      makeTie({ id: 2, part_number: 'BAR-1.5-6061', status: 'cancelled', work_order_operation_id: null }),
    ]);
    renderPanel();

    await screen.findByText('BAR-1.5-6061');
    expect(rowFor('BAR-1.5-6061').className).toContain('opacity-50');
    expect(rowFor('SHT-.125-304').className).not.toContain('opacity-50');
    expect(screen.getByText(/Cancelled ties stay listed/i)).toBeInTheDocument();
  });

  it('does NOT paint an open tie with the defect-red status colour', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie(),
      makeTie({ id: 2, part_number: 'BAR-1.5-6061', status: 'cancelled' }),
    ]);
    renderPanel();
    await screen.findByText('BAR-1.5-6061');

    const openChip = within(rowFor('SHT-.125-304')).getByText('open');
    expect(openChip.className).not.toContain('red');
    expect(openChip.className).toContain('emerald');
    // …and the two states are visually distinguishable.
    const cancelledChip = within(rowFor('BAR-1.5-6061')).getByText('cancelled');
    expect(cancelledChip.className).not.toBe(openChip.className);
  });

  it('derives "fully consumed" from the quantities, never from status', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 3, qty_planned: 3 })]);
    renderPanel();

    await screen.findByText('SHT-.125-304');
    // Still `open` — nothing ever writes `closed` — yet reported as consumed.
    expect(within(rowFor('SHT-.125-304')).getByText('open')).toBeInTheDocument();
    expect(screen.getByText('Fully consumed')).toBeInTheDocument();
  });

  it('labels qty_consumed as a REPORTED cache, not a compliance figure', async () => {
    renderPanel();
    await screen.findByText('SHT-.125-304');

    expect(screen.getByText('Consumed (reported)')).toBeInTheDocument();
    expect(screen.getByTitle(/ledger is the authoritative consumed record/i)).toBeInTheDocument();
  });

  it('distinguishes a DETACHED tie from one that was always work-order-scoped', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie({ work_order_operation_id: null, detached_from_operation_id: 71 }),
      makeTie({ id: 2, part_number: 'BAR-1.5-6061', work_order_operation_id: null }),
    ]);
    renderPanel();

    await screen.findByText('BAR-1.5-6061');
    expect(screen.getByText('was Op 71 — superseded by re-import')).toBeInTheDocument();
    expect(screen.getByText('Whole work order')).toBeInTheDocument();
  });

  it('shows an unpinned tie as FIFO and names the lot when one is pinned', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie(),
      makeTie({ id: 2, part_number: 'BAR-1.5-6061', pinned_inventory_item_id: 9, pinned_lot_number: 'HEAT-7741' }),
    ]);
    renderPanel();

    await screen.findByText('BAR-1.5-6061');
    expect(screen.getByText('FIFO')).toBeInTheDocument();
    expect(screen.getByText('HEAT-7741')).toBeInTheDocument();
  });

  it('offers a Retry that re-runs the fetch when the load fails', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations
      .mockRejectedValueOnce({ response: { data: { detail: 'Material ties unavailable' } } })
      .mockResolvedValueOnce([makeTie()]);
    renderPanel();

    expect(await screen.findByText('Material ties unavailable')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('SHT-.125-304')).toBeInTheDocument();
  });
});

describe('MaterialTiesPanel — gated mutations', () => {
  it('hides edit and untie from a viewer without work_orders:edit', async () => {
    renderPanel(false);
    await screen.findByText('SHT-.125-304');

    expect(screen.queryByLabelText(/Edit the SHT/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Untie SHT/)).not.toBeInTheDocument();
  });

  it('offers no actions on a cancelled tie — there is nothing left to gate', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ status: 'cancelled' })]);
    renderPanel();
    await screen.findByText('SHT-.125-304');

    expect(screen.queryByLabelText(/Edit the SHT/)).not.toBeInTheDocument();
  });

  it('surfaces an untie refusal VERBATIM and leaves the row exactly as it was', async () => {
    const user = userEvent.setup();
    mockApi.deleteMaterialAllocation.mockRejectedValue({
      response: { data: { detail: 'Cannot untie material that has already been consumed' } },
    });
    renderPanel();
    await screen.findByText('SHT-.125-304');

    await user.click(screen.getByLabelText('Untie SHT-.125-304 from this work order'));
    await user.click(screen.getByRole('button', { name: 'Untie material' }));

    expect(await screen.findByTestId('wo-tie-untie-error')).toHaveTextContent(
      'Cannot untie material that has already been consumed'
    );
    // NON-optimistic: nothing was re-read and the tie is still open.
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(1);
    expect(within(rowFor('SHT-.125-304')).getByText('open')).toBeInTheDocument();
  });

  it('never renders a structured 409 detail as "[object Object]"', async () => {
    const user = userEvent.setup();
    mockApi.deleteMaterialAllocation.mockRejectedValue({
      response: { data: { detail: { code: 'ALLOCATION_CONSUMED', detail: 'Consumption already posted' } } },
    });
    renderPanel();
    await screen.findByText('SHT-.125-304');

    await user.click(screen.getByLabelText('Untie SHT-.125-304 from this work order'));
    await user.click(screen.getByRole('button', { name: 'Untie material' }));

    const alert = await screen.findByTestId('wo-tie-untie-error');
    expect(alert).toHaveTextContent('Consumption already posted');
    expect(alert.textContent).not.toContain('[object Object]');
  });

  it('re-reads from the server after a successful untie rather than patching state', async () => {
    const user = userEvent.setup();
    mockApi.deleteMaterialAllocation.mockResolvedValue(makeTie({ status: 'cancelled' }));
    mockApi.getMaterialAllocations
      .mockResolvedValueOnce([makeTie()])
      .mockResolvedValueOnce([makeTie({ status: 'cancelled' })]);
    renderPanel();
    await screen.findByText('SHT-.125-304');

    await user.click(screen.getByLabelText('Untie SHT-.125-304 from this work order'));
    await user.click(screen.getByRole('button', { name: 'Untie material' }));

    await waitFor(() => expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('cancelled')).toBeInTheDocument();
  });

  it('PATCHes only what changed and surfaces the refusal inline on a rejection', async () => {
    const user = userEvent.setup();
    mockApi.updateMaterialAllocation.mockRejectedValue({
      response: { data: { detail: 'qty_planned cannot be lowered below qty_consumed' } },
    });
    renderPanel();
    await screen.findByText('SHT-.125-304');

    await user.click(screen.getByLabelText('Edit the SHT-.125-304 tie'));
    const planned = await screen.findByLabelText('Planned quantity (EA)');
    await user.clear(planned);
    await user.type(planned, '1');
    await user.click(screen.getByRole('button', { name: 'Save tie' }));

    expect(mockApi.updateMaterialAllocation).toHaveBeenCalledWith(42, 1, { qty_planned: 1 });
    expect(await screen.findByTestId('wo-tie-edit-error')).toHaveTextContent(
      'qty_planned cannot be lowered below qty_consumed'
    );
    // The dialog stays open on a refusal — reflect only what the server returned.
    expect(screen.getByRole('button', { name: 'Save tie' })).toBeInTheDocument();
  });

  it('refuses to send an empty PATCH', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText('SHT-.125-304');

    await user.click(screen.getByLabelText('Edit the SHT-.125-304 tie'));
    await user.click(await screen.findByRole('button', { name: 'Save tie' }));

    expect(mockApi.updateMaterialAllocation).not.toHaveBeenCalled();
    expect(await screen.findByTestId('wo-tie-edit-error')).toHaveTextContent('Nothing changed.');
  });

  it('offers the pin-clearing control only on a pinned tie', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie({ pinned_inventory_item_id: 9, pinned_lot_number: 'HEAT-7741' }),
    ]);
    mockApi.updateMaterialAllocation.mockResolvedValue(makeTie());
    renderPanel();
    await screen.findByText('SHT-.125-304');

    await user.click(screen.getByLabelText('Edit the SHT-.125-304 tie'));
    await user.click(await screen.findByLabelText(/Clear the lot pin/));
    await user.click(screen.getByRole('button', { name: 'Save tie' }));

    expect(mockApi.updateMaterialAllocation).toHaveBeenCalledWith(42, 1, { clear_pinned_inventory_item: true });
  });
});
