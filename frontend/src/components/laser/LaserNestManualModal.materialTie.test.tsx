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
