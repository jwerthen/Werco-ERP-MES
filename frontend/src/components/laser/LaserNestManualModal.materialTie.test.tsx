/**
 * LaserNestManualModal — sheet-part tie (material consumption, PR 2).
 *
 * Create path: the tie rides on the manual-create body (the backend ties the
 * operation it creates) — no client-side follow-up POST. An untied nest POSTs
 * the exact body it did before this feature existed.
 *
 * Edit path: `LaserNestInfo` carries no operation id, so the tie is only
 * addressable when the caller passes `workOrderOperationId`. When it does, the
 * modal reconciles the tie through the material-allocations API — NON-optimistic
 * throughout, because every verb there is server-gated (409 untie-after-
 * consumption, 422 over-consumed quantity).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LaserNestManualModal from './LaserNestManualModal';
import { LaserNestInfo, MaterialAllocation, Part } from '../../types';
import { ToastProvider } from '../ui';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    createManualLaserNest: jest.fn(),
    updateLaserNest: jest.fn(),
    uploadDocument: jest.fn(),
    attachLaserNestDocument: jest.fn(),
    extractLaserNestFromPdf: jest.fn(),
    getMaterials: jest.fn(),
    getMaterialAllocations: jest.fn(),
    createMaterialAllocation: jest.fn(),
    updateMaterialAllocation: jest.fn(),
    deleteMaterialAllocation: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const part = (overrides: Partial<Part>): Part => ({
  id: 1,
  version: 1,
  part_number: 'MAT-1',
  revision: 'A',
  name: 'Material',
  part_type: 'raw_material',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const SHEET_304 = part({ id: 31, part_number: 'SHT-304-125', name: '304 SS 0.125 Sheet' });
const SHEET_AL = part({ id: 32, part_number: 'SHT-AL-090', name: 'AL 6061 0.090 Sheet' });

/**
 * The catalog rows that make "material" the wrong default for a field labelled
 * "Sheet part". The purchased and hardware rows are what `/materials` really
 * serves; the manufactured one is the DEFENSIVE case — that endpoint cannot
 * serve it today, and the picker excludes it anyway because the prop is typed
 * `Part[]` and a future caller could hand it a wider list.
 */
// Real sheet stock the importers typed `purchased` — the reason the narrowed
// view needs an escape hatch rather than being a restriction.
const PURCHASED_SHEET = part({
  id: 33,
  part_number: 'SHT-CS-060',
  name: 'CS 0.060 Sheet',
  part_type: 'purchased',
});
// Passes the text-only sheet heuristic on the word "Sheet", and is a box of
// screws.
const SHEET_METAL_SCREW = part({
  id: 34,
  part_number: 'HW-SMS-8',
  name: 'Sheet metal screw #8',
  part_type: 'hardware',
});
// A part the shop PRODUCES: excluded at both tiers, with no escape hatch.
const MANUFACTURED_BRACKET = part({
  id: 35,
  part_number: 'BRK-100',
  name: 'Bracket, sheet metal',
  part_type: 'manufactured',
});

const NEST: LaserNestInfo = {
  id: 3,
  nest_name: 'Nest A',
  cnc_number: '1234',
  planned_runs: 4,
  completed_runs: 1,
  remaining_runs: 3,
};

const tie = (overrides: Partial<MaterialAllocation> = {}): MaterialAllocation => ({
  id: 900,
  work_order_id: 42,
  work_order_operation_id: 501,
  operation_number: '10',
  detached_from_operation_id: null,
  part_id: 31,
  part_number: 'SHT-304-125',
  part_name: '304 SS 0.125 Sheet',
  source: 'nest',
  status: 'open',
  qty_per_run: 2,
  qty_planned: 8,
  unit_of_measure: 'EA',
  qty_consumed: 0,
  pinned_inventory_item_id: null,
  pinned_lot_number: null,
  notes: null,
  created_by: 1,
  created_at: '2026-07-01T12:00:00Z',
  updated_at: '2026-07-01T12:00:00Z',
  ...overrides,
});

const fill = (label: RegExp, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getMaterials.mockResolvedValue([SHEET_304, SHEET_AL]);
  mockApi.getMaterialAllocations.mockResolvedValue([]);
  mockApi.extractLaserNestFromPdf.mockResolvedValue({
    cnc_number: null,
    material: null,
    thickness: null,
    sheet_size: null,
    planned_runs: null,
    confidence: 'high',
    source: 'ai',
    warning: null,
  });
});

describe('create path', () => {
  it('sends the tie on the create body — no follow-up allocation POST', async () => {
    mockApi.createManualLaserNest.mockResolvedValue({
      id: 7,
      nest_name: '8001',
      cnc_number: '8001',
      planned_runs: 5,
      completed_runs: 0,
      remaining_runs: 5,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await screen.findByLabelText(/sheet part/i);

    fill(/cnc number/i, '8001');
    fill(/qty to cut/i, '5');
    fill(/sheet part/i, '31');
    fill(/sheets per run/i, '2');
    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    await waitFor(() => expect(mockApi.createManualLaserNest).toHaveBeenCalledTimes(1));
    expect(mockApi.createManualLaserNest).toHaveBeenCalledWith(
      42,
      expect.objectContaining({ cnc_number: '8001', planned_runs: 5, material_part_id: 31, qty_per_run: 2 })
    );
    expect(mockApi.createMaterialAllocation).not.toHaveBeenCalled();
  });

  it('an untied nest POSTs no tie keys at all', async () => {
    mockApi.createManualLaserNest.mockResolvedValue({
      id: 8,
      nest_name: '8002',
      cnc_number: '8002',
      planned_runs: 1,
      completed_runs: 0,
      remaining_runs: 1,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await screen.findByLabelText(/sheet part/i);

    fill(/cnc number/i, '8002');
    fill(/qty to cut/i, '1');
    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    await waitFor(() => expect(mockApi.createManualLaserNest).toHaveBeenCalledTimes(1));
    const [, body] = mockApi.createManualLaserNest.mock.calls[0];
    expect(body).not.toHaveProperty('material_part_id');
    expect(body).not.toHaveProperty('qty_per_run');
  });

  it('refuses a sheet part with no per-run quantity rather than letting the API default it', async () => {
    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await screen.findByLabelText(/sheet part/i);

    fill(/cnc number/i, '8003');
    fill(/qty to cut/i, '1');
    fill(/sheet part/i, '31');
    fill(/sheets per run/i, '');
    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    expect(await screen.findByText(/enter sheets per run for the tied sheet part/i)).toBeInTheDocument();
    expect(mockApi.createManualLaserNest).not.toHaveBeenCalled();
  });

  it('hides the tie controls entirely when no material parts load', async () => {
    mockApi.getMaterials.mockRejectedValue(new Error('materials down'));

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());

    expect(screen.queryByLabelText(/sheet part/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/sheets per run/i)).not.toBeInTheDocument();
  });
});

/**
 * The sheet-part list defaults to RAW STOCK.
 *
 * `/materials` serves all four material-supply types and three of them
 * (`purchased`, `hardware`, `consumable`) are bought COMPONENTS — the seeded
 * catalog types bolts and nuts as `purchased` — so an unnarrowed list puts every
 * nut and abrasive pad under a field labelled "Sheet part". The narrowing is a
 * DEFAULT: real sheet is sometimes typed `purchased`, so the escape hatch has to
 * exist. The exclusion of parts the shop PRODUCES is not a default and has no
 * escape hatch.
 */
describe('sheet-part list: raw stock by default', () => {
  /** Every option in the native <select>, minus the "(none)" row. */
  const listedOptions = (): string[] =>
    Array.from(
      (screen.getByLabelText(/sheet part/i) as HTMLSelectElement).options
    )
      .filter((option) => option.value !== '')
      .map((option) => option.textContent ?? '');

  const openModal = async () => {
    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await screen.findByLabelText(/sheet part/i);
    await waitFor(() => expect(listedOptions().length).toBeGreaterThan(0));
  };

  const toggle = () => screen.getByRole('button', { name: /^Show (all materials \(\d+ more\)|raw stock only)$/ });

  beforeEach(() => {
    mockApi.getMaterials.mockResolvedValue([
      SHEET_304,
      PURCHASED_SHEET,
      SHEET_METAL_SCREW,
      MANUFACTURED_BRACKET,
    ]);
  });

  it('lists only raw stock, and offers the rest behind a toggle', async () => {
    await openModal();

    expect(listedOptions()).toEqual(['SHT-304-125 — 304 SS 0.125 Sheet']);
    // Two hidden: the purchased sheet and the hardware screw. The manufactured
    // bracket is excluded, not hidden, so it is not in the count.
    expect(toggle()).toHaveTextContent('Show all materials (2 more)');
  });

  it('reveals exactly what the count advertises', async () => {
    await openModal();

    const before = listedOptions().length;
    fireEvent.click(toggle());

    expect(listedOptions()).toHaveLength(before + 2);
    expect(listedOptions()).toEqual(
      expect.arrayContaining(['SHT-CS-060 — CS 0.060 Sheet', 'HW-SMS-8 — Sheet metal screw #8'])
    );
    expect(toggle()).toHaveTextContent('Show raw stock only');
  });

  it('never offers a part the shop PRODUCES, at either tier', async () => {
    await openModal();

    expect(listedOptions()).not.toContain('BRK-100 — Bracket, sheet metal');
    fireEvent.click(toggle());
    expect(listedOptions()).not.toContain('BRK-100 — Bracket, sheet metal');
  });

  it('does not blank a pick made through the escape hatch when the filter narrows again', async () => {
    // A <select> whose value is absent from its options renders BLANK, so
    // without the pin this sequence silently discards the planner's choice —
    // and on a create it would POST an untied nest they believe is tied.
    await openModal();

    fireEvent.click(toggle());
    fill(/sheet part/i, '33');
    expect(screen.getByLabelText(/sheet part/i)).toHaveValue('33');

    fireEvent.click(toggle());

    expect(screen.getByLabelText(/sheet part/i)).toHaveValue('33');
    expect(listedOptions()).toContain('SHT-CS-060 — CS 0.060 Sheet');
    // …and it is not double-counted as still hidden.
    expect(toggle()).toHaveTextContent('Show all materials (1 more)');
  });

  it('keeps the tie controls when the catalog holds no raw stock at all', async () => {
    // A shop whose sheet is all typed `purchased` (both importers fall back to
    // it) would otherwise lose the tie controls AND the toggle that reveals
    // them — the field would simply vanish.
    mockApi.getMaterials.mockResolvedValue([PURCHASED_SHEET, SHEET_METAL_SCREW]);

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    const select = await screen.findByLabelText(/sheet part/i);
    await waitFor(() => expect(screen.getByRole('button', { name: /Show all materials/ })).toBeInTheDocument());

    expect(select).toBeInTheDocument();
    expect(listedOptions()).toEqual([]);

    fireEvent.click(toggle());
    expect(listedOptions()).toEqual(['SHT-CS-060 — CS 0.060 Sheet', 'HW-SMS-8 — Sheet metal screw #8']);
  });

  it('still hides the controls when the material load fails outright', async () => {
    // The degraded-load behavior must survive the new "hidden count counts too"
    // rule: nothing loaded means nothing to show and nothing to reveal.
    mockApi.getMaterials.mockRejectedValue(new Error('materials down'));

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());

    expect(screen.queryByLabelText(/sheet part/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show all materials/ })).not.toBeInTheDocument();
  });

  it('keeps an existing tie to non-raw stock selected on the edit path', async () => {
    // The nest is really tied to the purchased sheet. A blank field here reads
    // as "this nest is untied" when it is not, and saving would then untie it.
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, part_id: 33, part_number: 'SHT-CS-060', part_name: 'CS 0.060 Sheet' }),
    ]);

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={NEST}
        workOrderOperationId={501}
        onClose={jest.fn()}
        onSaved={jest.fn()}
      />
    );

    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('33'));
    expect(listedOptions()).toContain('SHT-CS-060 — CS 0.060 Sheet');
    expect(toggle()).toHaveTextContent('Show all materials (1 more)');

    // Widening and narrowing again leaves it where it was.
    fireEvent.click(toggle());
    fireEvent.click(toggle());
    expect(screen.getByLabelText(/sheet part/i)).toHaveValue('33');
  });
});

describe('edit path', () => {
  it('renders no tie controls when the caller cannot say which operation backs the nest', async () => {
    render(<LaserNestManualModal open workOrderId={42} nest={NEST} onClose={jest.fn()} onSaved={jest.fn()} />);
    await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());

    expect(screen.queryByLabelText(/sheet part/i)).not.toBeInTheDocument();
    // Without an operation id there is nothing to read a tie from.
    expect(mockApi.getMaterialAllocations).not.toHaveBeenCalled();
  });

  it('pre-fills from the OPEN operation-scoped tie and PATCHes a changed quantity', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, qty_per_run: 2, qty_planned: 8 }),
      tie({ id: 901, work_order_operation_id: 502, part_id: 32 }),
      tie({ id: 902, work_order_operation_id: 501, part_id: 32, status: 'cancelled' }),
    ]);
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      cnc_number: '1234',
      planned_runs: 4,
      completed_runs: 1,
      remaining_runs: 3,
    });
    mockApi.updateMaterialAllocation.mockResolvedValue(tie({ qty_per_run: 3, qty_planned: 12 }));

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={NEST}
        workOrderOperationId={501}
        onClose={jest.fn()}
        onSaved={jest.fn()}
      />
    );

    // Live ties only, and the cancelled row on the same operation is ignored.
    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledWith(42, false);
    expect(screen.getByLabelText(/sheets per run/i)).toHaveValue(2);

    fill(/sheets per run/i, '3');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.updateMaterialAllocation).toHaveBeenCalledTimes(1));
    // qty_planned tracks per-run x planned runs (4).
    expect(mockApi.updateMaterialAllocation).toHaveBeenCalledWith(42, 900, { qty_per_run: 3, qty_planned: 12 });
    expect(mockApi.createMaterialAllocation).not.toHaveBeenCalled();
    expect(mockApi.deleteMaterialAllocation).not.toHaveBeenCalled();
  });

  it('unties when the sheet part is cleared', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie()]);
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      planned_runs: 4,
      completed_runs: 1,
      remaining_runs: 3,
    });
    mockApi.deleteMaterialAllocation.mockResolvedValue(tie({ status: 'cancelled' }));

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={NEST}
        workOrderOperationId={501}
        onClose={jest.fn()}
        onSaved={jest.fn()}
      />
    );
    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));

    fill(/sheet part/i, '');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.deleteMaterialAllocation).toHaveBeenCalledWith(42, 900));
    expect(mockApi.createMaterialAllocation).not.toHaveBeenCalled();
  });

  it('swapping the sheet part unties and re-ties (part_id is fixed at creation)', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie()]);
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      planned_runs: 4,
      completed_runs: 1,
      remaining_runs: 3,
    });
    mockApi.deleteMaterialAllocation.mockResolvedValue(tie({ status: 'cancelled' }));
    mockApi.createMaterialAllocation.mockResolvedValue(tie({ id: 950, part_id: 32 }));

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={NEST}
        workOrderOperationId={501}
        onClose={jest.fn()}
        onSaved={jest.fn()}
      />
    );
    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));

    fill(/sheet part/i, '32');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalledTimes(1));
    expect(mockApi.deleteMaterialAllocation).toHaveBeenCalledWith(42, 900);
    expect(mockApi.createMaterialAllocation).toHaveBeenCalledWith(42, {
      part_id: 32,
      work_order_operation_id: 501,
      source: 'nest',
      qty_per_run: 2,
      qty_planned: 8,
    });
  });

  it('creates a tie on an untied nest without touching delete', async () => {
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      planned_runs: 4,
      completed_runs: 1,
      remaining_runs: 3,
    });
    mockApi.createMaterialAllocation.mockResolvedValue(tie({ id: 960 }));

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={NEST}
        workOrderOperationId={501}
        onClose={jest.fn()}
        onSaved={jest.fn()}
      />
    );
    await screen.findByLabelText(/sheet part/i);

    fill(/sheet part/i, '31');
    fill(/sheets per run/i, '2');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalledTimes(1));
    expect(mockApi.deleteMaterialAllocation).not.toHaveBeenCalled();
  });

  it('surfaces the server refusal verbatim and keeps the modal open (non-optimistic)', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ qty_consumed: 4 })]);
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      planned_runs: 4,
      completed_runs: 1,
      remaining_runs: 3,
    });
    mockApi.deleteMaterialAllocation.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Cannot untie: 4.0 EA already consumed against this allocation. Reverse consumption first.' },
      },
    });
    const onClose = jest.fn();
    const onSaved = jest.fn();

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={NEST}
        workOrderOperationId={501}
        onClose={onClose}
        onSaved={onSaved}
      />
    );
    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));

    fill(/sheet part/i, '');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByText(/4\.0 EA already consumed against this allocation/i)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // The form keeps the planner's edit so they can retry or put it back; the
    // tie itself is untouched — nothing here pretended the untie landed.
    expect(screen.getByLabelText(/sheet part/i)).toHaveValue('');
    expect(mockApi.createMaterialAllocation).not.toHaveBeenCalled();
  });
});

/**
 * A LEGACY tie whose part is one the shop PRODUCES, on the EDIT path.
 *
 * Every other value in this picker comes from `/materials`, which serves only
 * the material-supply types — so the exclusion has nothing to drop and the
 * ordinary catalog path needs no check of its own. The tie read off
 * `GET /work-orders/{id}/materials` is the exception: it names whatever part it
 * was created against, including one created before the server started refusing
 * them. Left alone it was pre-selected via `setValue` AND listed as a pickable
 * "Sheet part" through the "keep an existing tie selectable" branch, which is
 * the same leak the import wizard closes on its own pre-fill path.
 *
 * The refusal has three parts and dropping any one re-opens the path: the field
 * is left un-selected, the part is NOT offered as an option at either tier, and
 * a WARNING toast says so — `success` would hide a shortfall the planner must
 * act on, and `error` would claim a failure that did not happen (the nest and
 * its tie both read fine).
 */
describe('edit path: a legacy tie to a part the shop PRODUCES', () => {
  const PRODUCED_TIE = {
    part_id: 35,
    part_number: 'BRK-100',
    part_name: 'Bracket, sheet metal',
    part_type: 'manufactured' as const,
  };

  /** Every option in the native <select>, minus the "(none)" row. */
  const listedOptions = (): string[] =>
    Array.from((screen.getByLabelText(/sheet part/i) as HTMLSelectElement).options)
      .filter((option) => option.value !== '')
      .map((option) => option.textContent ?? '');

  const renderEdit = () =>
    render(
      <ToastProvider>
        <LaserNestManualModal
          open
          workOrderId={42}
          nest={NEST}
          workOrderOperationId={501}
          onClose={jest.fn()}
          onSaved={jest.fn()}
        />
      </ToastProvider>
    );

  it('leaves the sheet part un-selected and warns, instead of pre-filling it', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, ...PRODUCED_TIE })]);

    renderEdit();

    const warning = await screen.findByRole('alert');
    expect(warning).toHaveTextContent(
      'This nest is tied to a part that is not stock material — re-pick the sheet part before saving.'
    );
    expect(screen.getByLabelText(/sheet part/i)).toHaveValue('');
  });

  it('does not offer the produced part in the picker either, at either tier', async () => {
    // Pinning it in — the mechanism that rightly keeps an unlisted SHEET tie
    // selectable — would make this bad tie one click from being re-committed.
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, ...PRODUCED_TIE })]);
    mockApi.getMaterials.mockResolvedValue([SHEET_304, PURCHASED_SHEET]);

    renderEdit();
    await screen.findByRole('alert');

    expect(listedOptions()).toEqual(['SHT-304-125 — 304 SS 0.125 Sheet']);
    fireEvent.click(screen.getByRole('button', { name: /show all materials/i }));
    expect(listedOptions()).not.toContain('BRK-100 — Bracket, sheet metal');
  });

  it('carries the planned per-run across, so a re-pick keeps the quantity', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, qty_per_run: 3, ...PRODUCED_TIE })]);

    renderEdit();
    await screen.findByRole('alert');

    // The quantity is a plan figure, not a part — it still applies to whatever
    // sheet replaces the bad one.
    expect(screen.getByLabelText(/sheets per run/i)).toHaveValue(3);
  });

  it('re-picks as an untie-then-re-tie, never a second tie on the same operation', async () => {
    // The tie stays in state even though the field is blank. That is what makes
    // a re-pick the ordinary swap: without it this would POST a second, live
    // allocation alongside the bad one and the operation would consume twice.
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, ...PRODUCED_TIE })]);
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      cnc_number: '1234',
      planned_runs: 4,
      completed_runs: 1,
      remaining_runs: 3,
    });
    mockApi.deleteMaterialAllocation.mockResolvedValue(undefined as never);
    mockApi.createMaterialAllocation.mockResolvedValue(tie({ id: 901, part_id: 31 }));

    renderEdit();
    await screen.findByRole('alert');

    fill(/sheet part/i, '31');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalledTimes(1));
    expect(mockApi.deleteMaterialAllocation).toHaveBeenCalledWith(42, 900);
    expect(mockApi.createMaterialAllocation).toHaveBeenCalledWith(
      42,
      expect.objectContaining({ part_id: 31, work_order_operation_id: 501 })
    );
  });

  it('pre-fills exactly as before when the server sends no part_type at all', async () => {
    // THE compatibility case. An older API omits `part_type` entirely; reading
    // absent as "suspect" would silently blank a live, legitimate tie the moment
    // the client ran ahead of the server. `tie()` omits the field, which is
    // precisely the older-server shape.
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, part_id: 31 })]);
    expect(tie({ id: 900 })).not.toHaveProperty('part_type');

    renderEdit();

    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));
    expect(screen.queryByText(/not stock material/i)).not.toBeInTheDocument();
  });

  it('says nothing when the tie is ordinary stock', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, part_id: 31, part_type: 'raw_material' }),
    ]);

    renderEdit();

    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));
    expect(screen.queryByText(/not stock material/i)).not.toBeInTheDocument();
  });
});

/**
 * ...AND THAT TIE IS NEVER DROPPED SILENTLY.
 *
 * The refusal above leaves the field blank while the tie is still live and
 * still in `existingTie` — and `reconcileTie` reads a blank field as "untie
 * this nest". Correct when the planner cleared it; a trap when the MODAL did.
 * A planner who opened this dialog only to fix a CNC number saved, destroyed a
 * live allocation, and saw nothing but a toast that had already timed out.
 *
 * The guard has three parts and each is pinned below: a PERSISTENT notice
 * beside the field (the toast is gone in four seconds and never said what
 * saving would do), a ConfirmDialog on the save that would drop it, and tie
 * controls that stay on screen even when `/materials` is unreachable — which
 * was the state where the planner was told to re-pick with the field, the
 * toggle and the notice all hidden.
 *
 * What it deliberately does NOT do is block the save. The nest PATCH is a
 * server verb with no such rule, an unrelated one-character edit must not be
 * held hostage to a material decision, and with `/materials` down a block would
 * be unsatisfiable — refusing to save while offering no way to comply.
 */
describe('edit path: a legacy produced-part tie is never dropped by accident', () => {
  const PRODUCED_TIE = {
    part_id: 35,
    part_number: 'BRK-100',
    part_name: 'Bracket, sheet metal',
    part_type: 'manufactured' as const,
  };

  const NEST_PATCHED = {
    id: 3,
    nest_name: 'Nest A',
    cnc_number: '9999',
    planned_runs: 4,
    completed_runs: 1,
    remaining_runs: 3,
  };

  const renderEdit = () =>
    render(
      <ToastProvider>
        <LaserNestManualModal
          open
          workOrderId={42}
          nest={NEST}
          workOrderOperationId={501}
          onClose={jest.fn()}
          onSaved={jest.fn()}
        />
      </ToastProvider>
    );

  const confirmDialog = () => screen.findByRole('button', { name: /save and remove the tie/i });

  beforeEach(() => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, ...PRODUCED_TIE })]);
    mockApi.updateLaserNest.mockResolvedValue(NEST_PATCHED);
    mockApi.deleteMaterialAllocation.mockResolvedValue(undefined as never);
  });

  it('names the tie beside the field, persistently — not only in the load toast', async () => {
    renderEdit();
    await screen.findByRole('alert');

    // The notice is the select's own description, so it is read WITH the field
    // rather than once, at load, by a toast that is about to disappear.
    const select = screen.getByLabelText(/sheet part/i);
    const noticeId = select.getAttribute('aria-describedby');
    expect(noticeId).toBeTruthy();
    const notice = document.getElementById(noticeId!);
    expect(notice).toHaveTextContent('BRK-100 — Bracket, sheet metal');
    expect(notice).toHaveTextContent(/saving with this blank removes the tie/i);
  });

  it('asks before a save that would drop the tie, and fires nothing until confirmed', async () => {
    renderEdit();
    await screen.findByRole('alert');

    // The planner came here for the CNC number and never touched the tie.
    fill(/cnc number/i, '9999');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByText(/remove this nest's material tie\?/i)).toBeInTheDocument();
    // NOTHING has gone to the server yet — not even the nest PATCH the planner
    // actually asked for, because the confirm may still cancel the whole save.
    expect(mockApi.updateLaserNest).not.toHaveBeenCalled();
    expect(mockApi.deleteMaterialAllocation).not.toHaveBeenCalled();
  });

  it('cancelling leaves the tie alone and keeps the planner on the form', async () => {
    const onSaved = jest.fn();
    render(
      <ToastProvider>
        <LaserNestManualModal
          open
          workOrderId={42}
          nest={NEST}
          workOrderOperationId={501}
          onClose={jest.fn()}
          onSaved={onSaved}
        />
      </ToastProvider>
    );
    await screen.findByRole('alert');

    fill(/cnc number/i, '9999');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    fireEvent.click(await screen.findByRole('button', { name: /keep editing/i }));

    await waitFor(() =>
      expect(screen.queryByText(/remove this nest's material tie\?/i)).not.toBeInTheDocument()
    );
    expect(mockApi.deleteMaterialAllocation).not.toHaveBeenCalled();
    expect(mockApi.updateLaserNest).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    // The planner's edit survives the interruption.
    expect(screen.getByLabelText(/cnc number/i)).toHaveValue('9999');
  });

  it('confirming saves and unties — the drop stays possible, it just has to be chosen', async () => {
    renderEdit();
    await screen.findByRole('alert');

    fill(/cnc number/i, '9999');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    fireEvent.click(await confirmDialog());

    await waitFor(() => expect(mockApi.deleteMaterialAllocation).toHaveBeenCalledWith(42, 900));
    expect(mockApi.updateLaserNest).toHaveBeenCalledWith(3, expect.objectContaining({ cnc_number: '9999' }));
  });

  it('does NOT ask when the planner re-picked a real sheet — that is the ordinary swap', async () => {
    mockApi.createMaterialAllocation.mockResolvedValue(tie({ id: 901, part_id: 31 }));

    renderEdit();
    await screen.findByRole('alert');

    fill(/sheet part/i, '31');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/remove this nest's material tie\?/i)).not.toBeInTheDocument();
  });

  it('does NOT ask when the planner cleared an ordinary tie by hand', async () => {
    // Clearing the field is already an explicit act. The guard is for a blank
    // the MODAL wrote, not for every untie.
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, part_id: 31, part_type: 'raw_material' })]);

    renderEdit();
    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));

    fill(/sheet part/i, '');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.deleteMaterialAllocation).toHaveBeenCalledWith(42, 900));
    expect(screen.queryByText(/remove this nest's material tie\?/i)).not.toBeInTheDocument();
  });

  it('a refused untie keeps the dialog closed and shows the server sentence on the form', async () => {
    // Non-optimistic: the untie is server-GATED (409 once material has been
    // consumed), so a refusal must land the planner back on the form reading
    // what the server said — not on a dialog that already vanished.
    mockApi.deleteMaterialAllocation.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Cannot untie: 4.0 EA already consumed against this allocation.' },
      },
    });

    renderEdit();
    await screen.findByRole('alert');

    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    fireEvent.click(await confirmDialog());

    expect(await screen.findByText(/4\.0 EA already consumed against this allocation/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/remove this nest's material tie\?/i)).not.toBeInTheDocument()
    );
  });

  it('keeps the field, the toggle and the notice on screen when /materials is unreachable', async () => {
    // THE edge that made the warning unactionable: with no material loaded both
    // catalog terms are zero, so the tie controls vanished — the planner was
    // told to re-pick with nothing on screen to re-pick in, and `reconcileTie`
    // was skipped along with the controls, so the same nest behaved differently
    // depending on whether an unrelated read had succeeded.
    mockApi.getMaterials.mockRejectedValue(new Error('materials down'));

    renderEdit();
    await screen.findByRole('alert');

    const select = await screen.findByLabelText(/sheet part/i);
    expect(select).toBeInTheDocument();
    const notice = document.getElementById(select.getAttribute('aria-describedby')!);
    expect(notice).toHaveTextContent(/no material could be loaded to pick from/i);

    // And the save still goes through the confirm rather than through a hidden
    // control that quietly did nothing.
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    expect(await screen.findByText(/remove this nest's material tie\?/i)).toBeInTheDocument();
  });

  it('says nothing extra for an ordinary tie — no notice, no aria-describedby', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([tie({ id: 900, part_id: 31, part_type: 'raw_material' })]);

    renderEdit();
    await waitFor(() => expect(screen.getByLabelText(/sheet part/i)).toHaveValue('31'));

    expect(screen.getByLabelText(/sheet part/i)).not.toHaveAttribute('aria-describedby');
    expect(screen.queryByText(/removes the tie/i)).not.toBeInTheDocument();
  });
});
