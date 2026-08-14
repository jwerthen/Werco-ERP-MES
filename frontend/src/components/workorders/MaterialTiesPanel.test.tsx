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
 *  - edits/unties/returns are server-GATED, so they are NON-optimistic: the row
 *    does not change until the server confirms, refusals render verbatim, and an
 *    object `detail` never reaches the DOM as "[object Object]";
 *  - the panel is gated on work_orders:edit;
 *  - RETURN is offered on a CANCELLED tie that carries consumption — that is
 *    exactly the state the work-order hard-delete 409 points at, and hiding the
 *    verb there would leave that refusal with no self-service path;
 *  - the return dialog shows WHICH LOTS get credited before confirming, never
 *    shows the raw intent enum values, and never claims a full return unlocks
 *    nest re-import.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MaterialTiesPanel from './MaterialTiesPanel';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type {
  MaterialAllocation,
  MaterialConsumptionLine,
  MaterialReturnResult,
  WorkOrderOperation,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getMaterialAllocations: jest.fn(),
    updateMaterialAllocation: jest.fn(),
    deleteMaterialAllocation: jest.fn(),
    // PR 3 — the reasoned RETURN verb. Any suite that renders this panel (all
    // six WorkOrderDetail ones do) needs these two on its hand-written api mock.
    getMaterialAllocationConsumption: jest.fn(),
    returnMaterialAllocation: jest.fn(),
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

const makeOp = (overrides: Partial<WorkOrderOperation> = {}): WorkOrderOperation =>
  ({ id: 71, quantity_complete: 3, quantity_scrapped: 0, ...overrides } as WorkOrderOperation);

const renderPanel = (canEdit = true, operations?: WorkOrderOperation[], refreshToken?: number) =>
  render(
    <ToastProvider>
      <MaterialTiesPanel
        workOrderId={42}
        workOrderUpdatedAt="2026-07-24T14:05:00Z"
        refreshToken={refreshToken}
        canEdit={canEdit}
        operations={operations}
      />
    </ToastProvider>
  );

const rowFor = (partNumber: string) => screen.getByText(partNumber).closest('tr') as HTMLElement;

const LOTS: MaterialConsumptionLine[] = [
  { inventory_item_id: 91, lot_number: 'HEAT-7741', issued: 2, returned: 0, net: 2 },
  { inventory_item_id: 90, lot_number: 'HEAT-6620', issued: 3, returned: 1, net: 2 },
];

/**
 * A return response shaped like the one the SERVER actually sends.
 *
 * Built from `MaterialReturnResponse` (`backend/app/schemas/work_order_material.py`)
 * field for field, and typed as `MaterialReturnResult` so a drift between the two
 * fails `tsc` rather than passing silently. These mocks previously invented
 * `{ lines, qty_consumed, status, allocation }` — a payload with no `returned_lots`
 * and an `allocation` object the server has never sent — so the suite was green
 * against fiction while the panel read `result.lines` and always got `undefined`.
 * Anything asserting on a return response must go through this builder.
 */
const makeReturnResult = (overrides: Partial<MaterialReturnResult> = {}): MaterialReturnResult => ({
  allocation_id: 1,
  work_order_id: 42,
  part_id: 55,
  part_number: 'SHT-.125-304',
  intent: 'correct_over_consumption',
  unit_of_measure: 'EA',
  quantity_returned: 2,
  qty_consumed_before: 5,
  qty_consumed: 3,
  status: 'open',
  returned_lots: [
    {
      inventory_item_id: 91,
      lot_number: 'HEAT-7741',
      quantity: 2,
      unit_cost: 4.5,
      transaction_id: 5001,
      compensated_transaction_id: 4001,
    },
  ],
  ...overrides,
});

/** Open the return dialog for a tie that already carries consumption. */
const openReturnDialog = async (user: ReturnType<typeof userEvent.setup>, part = 'SHT-.125-304') => {
  await screen.findByText(part);
  await user.click(screen.getByLabelText(`Return consumed ${part} to stock`));
  return screen.findByRole('heading', { name: `Return material — ${part}` });
};

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getMaterialAllocations.mockResolvedValue([makeTie()]);
  mockApi.getMaterialAllocationConsumption.mockResolvedValue(LOTS);
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

  it('states that an operation-scoped tie deducts at OPERATION completion, and never per run', async () => {
    // The panel spans both scopes, so it has to name both timings: an
    // operation-scoped tie deducts as its own operation closes (a per-nest
    // laser WO draws nest by nest), a whole-work-order tie at job completion.
    // Neither is per-run.
    renderPanel();
    await screen.findByText('SHT-.125-304');

    const subtitle = screen.getByText(/A tie scoped to an operation is deducted when that/i);
    expect(subtitle).toHaveTextContent('completes — not per run');
    expect(subtitle).toHaveTextContent('A whole-work-order tie drains when the work order finishes');
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

  it('offers no actions on a cancelled tie that consumed nothing', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ status: 'cancelled', qty_consumed: 0 })]);
    renderPanel();
    await screen.findByText('SHT-.125-304');

    expect(screen.queryByLabelText(/Edit the SHT/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Untie SHT/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Return consumed SHT/)).not.toBeInTheDocument();
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

describe('MaterialTiesPanel — the reasoned RETURN verb', () => {
  it('offers a return only once something has been consumed', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie({ qty_consumed: 0 }),
      makeTie({ id: 2, part_number: 'BAR-1.5-6061', qty_consumed: 2 }),
    ]);
    renderPanel();
    await screen.findByText('BAR-1.5-6061');

    expect(screen.queryByLabelText('Return consumed SHT-.125-304 to stock')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Return consumed BAR-1.5-6061 to stock')).toBeInTheDocument();
  });

  it('OFFERS the return on a CANCELLED tie — that is where the delete 409 points', async () => {
    // A consumed+cancelled tie is exactly the state a work-order hard delete
    // refuses on. Hiding the only verb that can unwind it would leave that
    // refusal a dead end.
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ status: 'cancelled', qty_consumed: 4 })]);
    renderPanel();
    await screen.findByText('SHT-.125-304');

    expect(screen.getByLabelText('Return consumed SHT-.125-304 to stock')).toBeInTheDocument();
    // …and still nothing else: there is no live tie left to edit or untie.
    expect(screen.queryByLabelText(/Edit the SHT/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Untie SHT/)).not.toBeInTheDocument();
  });

  it('hides the return from a viewer without work_orders:edit', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    renderPanel(false);
    await screen.findByText('SHT-.125-304');

    expect(screen.queryByLabelText(/^Return consumed SHT/)).not.toBeInTheDocument();
  });

  it('shows the consumed total and WHICH LOTS get credited before confirming', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    renderPanel();
    await openReturnDialog(user);

    expect(mockApi.getMaterialAllocationConsumption).toHaveBeenCalledWith(42, 1);
    expect(screen.getByTestId('wo-tie-return-consumed')).toHaveTextContent('4 EA');

    // Material goes back to the lots it came from — a spilled consumption is
    // several lots, and the user has to be able to see that.
    const table = await screen.findByTestId('wo-tie-return-lots');
    expect(within(table).getByText('HEAT-7741')).toBeInTheDocument();
    expect(within(table).getByText('HEAT-6620')).toBeInTheDocument();
    expect(screen.getByText(/no lot to choose/i)).toBeInTheDocument();
  });

  it('does not block the return when the per-lot read fails — the server picks the lots', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    mockApi.getMaterialAllocationConsumption.mockRejectedValue({
      response: { data: { detail: 'Consumption ledger unavailable' } },
    });
    renderPanel();
    await openReturnDialog(user);

    expect(await screen.findByTestId('wo-tie-return-lots-error')).toHaveTextContent(
      'Consumption ledger unavailable'
    );
    expect(screen.getByRole('button', { name: 'Return material' })).toBeEnabled();
  });

  it('names the two intents in plain language and never shows the raw enum values', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    renderPanel();
    const heading = await openReturnDialog(user);
    const dialog = heading.closest('div[role="dialog"]') ?? document.body;

    expect(screen.getByLabelText(/Correct an over-count/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Return everything and untie/)).toBeInTheDocument();
    expect(dialog.textContent).not.toContain('correct_over_consumption');
    expect(dialog.textContent).not.toContain('return_and_untie');
  });

  it('never claims a full return unlocks nest re-import', async () => {
    // The guard is LEDGER-keyed: both the ISSUE and the RETURN rows still point
    // at the operation a rebuild would delete, so re-import stays refused and
    // the remedy is a new work order.
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    renderPanel();
    await openReturnDialog(user);

    expect(screen.getByText(/does NOT unlock nest re-import/i)).toBeInTheDocument();
    expect(screen.getByText(/remedy is a new work order/i)).toBeInTheDocument();
  });

  it('blocks a blank reason client-side without ever calling the server', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '2');
    await user.type(screen.getByLabelText(/Reason for the return/), '   ');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    expect(mockApi.returnMaterialAllocation).not.toHaveBeenCalled();
    expect(await screen.findByTestId('wo-tie-return-error')).toHaveTextContent('A reason is required');
  });

  it('posts a bounded correction with the trimmed reason and re-reads from the server', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations
      .mockResolvedValueOnce([makeTie({ qty_consumed: 5 })])
      .mockResolvedValueOnce([makeTie({ qty_consumed: 3 })]);
    mockApi.returnMaterialAllocation.mockResolvedValue(makeReturnResult());
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '2');
    await user.type(screen.getByLabelText(/Reason for the return/), '  two sheets never left the rack  ');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    await waitFor(() =>
      expect(mockApi.returnMaterialAllocation).toHaveBeenCalledWith(42, 1, {
        quantity: 2,
        intent: 'correct_over_consumption',
        reason: 'two sheets never left the rack',
      })
    );
    // NON-optimistic: the panel re-reads rather than patching qty_consumed itself.
    await waitFor(() => expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(2));
  });

  it('confirms with the LOTS the server credited, read off the real response shape', async () => {
    // The regression test for a whole defect class: the panel read `result.lines`,
    // a field `MaterialReturnResponse` has never carried, so the per-lot count was
    // permanently 0 and the confirmation silently degraded to a plausible-looking
    // message with no error anywhere. Nothing caught it because the mocks invented
    // the client's shape. Asserting on the LOT NAMES forces the toast to come from
    // `returned_lots`, so the same drift cannot recur unnoticed.
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 5 })]);
    mockApi.returnMaterialAllocation.mockResolvedValue(
      makeReturnResult({
        quantity_returned: 4,
        returned_lots: [
          {
            inventory_item_id: 91,
            lot_number: 'HEAT-7741',
            quantity: 3,
            unit_cost: 4.5,
            transaction_id: 5001,
            compensated_transaction_id: 4001,
          },
          {
            inventory_item_id: 90,
            lot_number: 'HEAT-6620',
            quantity: 1,
            unit_cost: 4.1,
            transaction_id: 5002,
            compensated_transaction_id: 4000,
          },
        ],
      })
    );
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '4');
    await user.type(screen.getByLabelText(/Reason for the return/), 'four sheets back on the rack');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    // Both source lots are named — a spilled consumption returns to the lots it
    // came off, and which ones is what an operator checks against the rack.
    const toast = await screen.findByText(/Returned 4 EA of SHT-\.125-304/);
    expect(toast).toHaveTextContent('HEAT-7741');
    expect(toast).toHaveTextContent('HEAT-6620');
  });

  it('reports the quantity the SERVER credited, not the one that was requested', async () => {
    // `quantity_returned` is the response's own field. Echoing the request would
    // hide any server-side clamp behind a confident-looking confirmation.
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 5 })]);
    mockApi.returnMaterialAllocation.mockResolvedValue(
      makeReturnResult({ quantity_returned: 2, returned_lots: [] })
    );
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '2');
    await user.type(screen.getByLabelText(/Reason for the return/), 'two sheets never left the rack');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    // No lots on the response => no lot clause invented.
    const toast = await screen.findByText(/Returned 2 EA of SHT-\.125-304/);
    expect(toast).not.toHaveTextContent(/lot/i);
  });

  it('sends the tie’s exact consumed float for return_and_untie, not the rounded display value', async () => {
    // The server takes `quantity == qty_consumed` as a confirmation value and
    // 422s anything else, so a re-parsed 2-decimal display string would be
    // refused on a float like 2.9999999999.
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 2.9999999999 })]);
    mockApi.returnMaterialAllocation.mockResolvedValue(
      makeReturnResult({
        intent: 'return_and_untie',
        quantity_returned: 2.9999999999,
        qty_consumed_before: 2.9999999999,
        qty_consumed: 0,
        status: 'cancelled',
      })
    );
    renderPanel();
    await openReturnDialog(user);

    await user.click(screen.getByLabelText(/Return everything and untie/));
    expect(screen.getByLabelText(/Quantity to return/)).toBeDisabled();
    await user.type(screen.getByLabelText(/Reason for the return/), 'job cancelled, stock back on the rack');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    await waitFor(() =>
      expect(mockApi.returnMaterialAllocation).toHaveBeenCalledWith(42, 1, {
        quantity: 2.9999999999,
        intent: 'return_and_untie',
        reason: 'job cancelled, stock back on the rack',
      })
    );
  });

  it('defaults a cancelled tie to "return everything" — there is no live tie to keep', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ status: 'cancelled', qty_consumed: 4 })]);
    renderPanel();
    await openReturnDialog(user);

    expect(screen.getByLabelText(/Return everything and untie/)).toBeChecked();
    expect(screen.getByLabelText(/Correct an over-count/)).not.toBeChecked();
  });

  it('surfaces the unbounded-correction 422 VERBATIM so the named intent reaches the user', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    mockApi.returnMaterialAllocation.mockRejectedValue({
      response: {
        data: {
          detail:
            'Returning 4 EA would leave 1 EA below this operation’s live target, and the engine would '
            + 're-consume it. Use return_and_untie to return everything and cancel the tie.',
        },
      },
    });
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '4');
    await user.type(screen.getByLabelText(/Reason for the return/), 'over-count walked back');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    const alert = await screen.findByTestId('wo-tie-return-error');
    expect(alert).toHaveTextContent('Use return_and_untie to return everything');
    // NON-optimistic: the dialog stays open and nothing was re-read.
    expect(screen.getByRole('button', { name: 'Return material' })).toBeInTheDocument();
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(1);
  });

  it('shows a loading state and NO optimistic figure while the return is in flight', async () => {
    // The return is a server-GATED action that moves stock, so per the optimistic-UI
    // convention it is strictly non-optimistic: the panel must never render a
    // qty_consumed the server has not confirmed. Optimism here would show material
    // back on the shelf that the bound check is about to refuse.
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    let release: (value: MaterialReturnResult) => void = () => {};
    mockApi.returnMaterialAllocation.mockReturnValue(
      new Promise<MaterialReturnResult>((resolve) => {
        release = resolve;
      })
    );
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '2');
    await user.type(screen.getByLabelText(/Reason for the return/), 'two sheets never left the rack');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    // In flight: the control is busy and the reported figure is still the server's last
    // confirmed value (4), not the 2 the return would leave behind.
    expect(await screen.findByRole('button', { name: /Returning/ })).toBeDisabled();
    expect(screen.getByTestId('wo-tie-return-consumed')).toHaveTextContent('4 EA');

    release(makeReturnResult({ quantity_returned: 2, qty_consumed_before: 4, qty_consumed: 2 }));
    await waitFor(() => expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(2));
  });

  it('never renders a structured refusal as "[object Object]"', async () => {
    const user = userEvent.setup();
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 4 })]);
    mockApi.returnMaterialAllocation.mockRejectedValue({
      response: { data: { detail: { code: 'LOT_UNAVAILABLE', detail: 'Source lot 91 no longer exists' } } },
    });
    renderPanel();
    await openReturnDialog(user);

    await user.type(screen.getByLabelText(/Quantity to return/), '2');
    await user.type(screen.getByLabelText(/Reason for the return/), 'miscount');
    await user.click(screen.getByRole('button', { name: 'Return material' }));

    const alert = await screen.findByTestId('wo-tie-return-error');
    expect(alert).toHaveTextContent('Source lot 91 no longer exists');
    expect(alert.textContent).not.toContain('[object Object]');
  });
});

describe('MaterialTiesPanel — over-consumption flag', () => {
  it('flags a tie holding more material than the operation\'s recorded production justifies', async () => {
    // The open loop an office reduce leaves: the supervisor walked a COMPLETE
    // operation back 5 -> 3 but never returned the 2 sheets. target = 1 x 3 = 3,
    // qty_consumed = 5, so 2 are still out with nothing else on the page saying so.
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 5, qty_planned: 5 })]);
    renderPanel(true, [makeOp({ id: 71, quantity_complete: 3, quantity_scrapped: 0 })]);

    const chip = await screen.findByTestId('tie-over-consumed-1');
    expect(chip).toHaveTextContent('+2 out');
    expect(chip.getAttribute('title')).toMatch(/Return material/i);
  });

  it('does NOT flag a tie whose consumption the production record still justifies', async () => {
    // Same numbers, squared up: target = 1 x (3 + 2 scrapped) = 5 == qty_consumed.
    // Scrap RAISES the target, so a scrapped run must not read as over-consumption.
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 5, qty_planned: 5 })]);
    renderPanel(true, [makeOp({ id: 71, quantity_complete: 3, quantity_scrapped: 2 })]);

    await screen.findByText('SHT-.125-304');
    expect(screen.queryByTestId('tie-over-consumed-1')).not.toBeInTheDocument();
  });

  it('stays silent when the target cannot be known rather than claiming squared up', async () => {
    // A tie whose operation is not among those passed (detached by a nest re-import).
    // `overConsumedQty` returns null, not 0 — an absent answer beats a confident wrong one.
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_consumed: 5, qty_planned: 5 })]);
    renderPanel(true, [makeOp({ id: 999 })]);

    await screen.findByText('SHT-.125-304');
    expect(screen.queryByTestId('tie-over-consumed-1')).not.toBeInTheDocument();
  });
});

describe('MaterialTiesPanel — the refreshToken freshness seam (PR 4.5)', () => {
  // A tie written from ANYWHERE ELSE on the page — the per-operation editor in
  // the Operations table is the first such door — does not touch
  // `work_orders.updated_at`, which was this panel's only other load dependency.
  // Without a second seam the list sitting directly beneath that table would
  // show neither the new row nor a changed plan until something unrelated
  // bumped the work order, which reads as "the tie did not save".

  it('re-reads when the token changes, without the work order changing', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie()]);
    const { rerender } = renderPanel(true, undefined, 0);
    await screen.findByText('SHT-.125-304');
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(1);

    mockApi.getMaterialAllocations.mockResolvedValue([makeTie(), makeTie({ id: 2, part_number: 'BAR-1.0-6061' })]);
    rerender(
      <ToastProvider>
        <MaterialTiesPanel
          workOrderId={42}
          workOrderUpdatedAt="2026-07-24T14:05:00Z"
          refreshToken={1}
          canEdit
        />
      </ToastProvider>
    );

    expect(await screen.findByText('BAR-1.0-6061')).toBeInTheDocument();
    await waitFor(() => expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(2));
  });

  it('is optional — an unchanged token does not re-read on every render', async () => {
    // The prop is optional so existing callers (and the six WorkOrderDetail
    // suites) keep working untouched, and a re-render for an unrelated reason
    // must not turn a detail page into a polling client.
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie()]);
    const { rerender } = renderPanel(true);
    await screen.findByText('SHT-.125-304');
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(1);

    rerender(
      <ToastProvider>
        <MaterialTiesPanel workOrderId={42} workOrderUpdatedAt="2026-07-24T14:05:00Z" canEdit />
      </ToastProvider>
    );
    await screen.findByText('SHT-.125-304');
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledTimes(1);
  });
});

/**
 * Scope label — the "Op Op 10" bug class.
 *
 * `MaterialAllocation.operation_number` is the same free-text column the kiosk
 * fix normalized; this panel hard-coded its own `Op ` prefix around it. It now
 * routes through the shared `utils/operationLabel` helper, and the panel's own
 * `Operation #{id}` fallback is preserved — an em-dash would leave a tie unable
 * to say which operation it is scoped to.
 */
describe('MaterialTiesPanel scope label', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders a stored "Op 10" as "Op 10" — never "Op Op 10"', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ operation_number: 'Op 10' })]);
    renderPanel();

    expect(await screen.findByText('Op 10')).toBeInTheDocument();
    expect(screen.queryByText(/Op\s+Op/i)).toBeNull();
  });

  it('normalizes the other stored spellings to the same "Op 10"', async () => {
    for (const stored of ['10', 'OP10', 'op-10', 'Operation 10']) {
      mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ operation_number: stored })]);
      const { unmount } = renderPanel();
      expect(await screen.findByText('Op 10')).toBeInTheDocument();
      unmount();
    }
  });

  it('falls back to "Operation #{id}", not the em-dash, when the number is blank', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie({ operation_number: null, work_order_operation_id: 71 }),
    ]);
    renderPanel();

    expect(await screen.findByText('Operation #71')).toBeInTheDocument();
    expect(screen.queryByText('Op —')).toBeNull();
  });
});
