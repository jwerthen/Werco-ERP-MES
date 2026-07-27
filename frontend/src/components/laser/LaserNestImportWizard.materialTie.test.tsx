/**
 * LaserNestImportWizard — sheet-part tie (material consumption, PR 2).
 *
 * The review grid grows a "Sheet part" + "Sheets/run" pair per nest, plus a
 * package-level "apply to all rows" default. The rules this file guards:
 *
 *  - the picker is fed by /materials (NOT /parts, which returns zero raw
 *    material), with an ON-HAND hint (never "available");
 *  - a tie is always an EXPLICIT pick — nothing is auto-matched from the
 *    AI-extracted `material` free text;
 *  - an UNTIED row's import payload stays byte-identical to the pre-feature
 *    one: no `material_part_id` key, no `qty_per_run` key;
 *  - a tied row must carry a per-run quantity > 0 (the API enforces `gt=0`);
 *  - a re-import pre-fills each row from the tie its nest already carries,
 *    because importing CANCELS and DETACHES every existing tie;
 *  - the consumed-tie 409 explains that there is no reversal verb yet.
 *
 * Copy invariant: consumption fires at WORK-ORDER completion, never per run.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import LaserNestImportWizard from './LaserNestImportWizard';
import { LaserNestPackagePreview, MaterialAllocation, Part } from '../../types';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewLaserNestPackage: jest.fn(),
    importLaserNestPackage: jest.fn(),
    previewLaserNestPackageStandalone: jest.fn(),
    importLaserNestPackageStandalone: jest.fn(),
    getWorkCenters: jest.fn(),
    getMaterials: jest.fn(),
    getInventorySummary: jest.fn(),
    getMaterialAllocations: jest.fn(),
    getWorkOrder: jest.fn(),
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
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const SHEET_304 = part({ id: 31, part_number: 'SHT-304-125', name: '304 SS 0.125 Sheet' });
const SHEET_AL = part({ id: 32, part_number: 'SHT-AL-090', name: 'AL 6061 0.090 Sheet' });

const preview: LaserNestPackagePreview = {
  package_name: 'nests.zip',
  nest_count: 2,
  total_planned_runs: 7,
  nests: [
    {
      source_file: 'sheet-1.pdf',
      nest_name: 'Sheet 1',
      cnc_number: '8001',
      cnc_file_name: null,
      planned_runs: 5,
      material: '304 SS',
      thickness: '0.125"',
      sheet_size: '48x96',
      confidence: 'high',
    },
    {
      source_file: 'sheet-2.pdf',
      nest_name: 'Sheet 2',
      cnc_number: '8002',
      cnc_file_name: null,
      planned_runs: 2,
      material: 'AL 6061',
      thickness: '0.090"',
      sheet_size: '48x96',
      confidence: 'high',
    },
  ],
};

const tie = (overrides: Partial<MaterialAllocation>): MaterialAllocation => ({
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
  qty_planned: 10,
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

/** Pick a ZIP, run Preview, and wait for the review grid + the loaded pickers. */
async function previewPackage() {
  const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
  fireEvent.change(screen.getByLabelText(/zip package/i), { target: { files: [zip] } });
  fireEvent.click(screen.getByRole('button', { name: /^preview$/i }));
  await screen.findByRole('button', { name: /^import 2 nests$/i });
  await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());
}

const optionLabels = (select: HTMLElement): string[] =>
  Array.from((select as HTMLSelectElement).options).map((option) => option.textContent ?? '');

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getWorkCenters.mockResolvedValue([]);
  mockApi.getMaterials.mockResolvedValue([SHEET_304, SHEET_AL]);
  mockApi.getInventorySummary.mockResolvedValue([{ part_id: 31, total_on_hand: 12 }]);
  mockApi.getMaterialAllocations.mockResolvedValue([]);
  mockApi.getWorkOrder.mockResolvedValue({ id: 42, operations: [] });
  mockApi.previewLaserNestPackage.mockResolvedValue(preview);
  mockApi.importLaserNestPackage.mockResolvedValue({ child_work_order: { id: 909 } });
});

describe('sheet-part picker', () => {
  it('feeds the picker from /materials with a bounded limit and an ON-HAND hint', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    // /materials, never /parts (which defaults item_group=engineering and would
    // return zero raw material), and ALWAYS an explicit limit — getMaterials
    // auto-paginates forever when it is omitted.
    expect(mockApi.getMaterials).toHaveBeenCalledWith(
      expect.objectContaining({ active_only: true, limit: expect.any(Number) })
    );
    // Read once per wizard open, never per row/keystroke.
    expect(mockApi.getInventorySummary).toHaveBeenCalledTimes(1);

    const rowSelect = await screen.findByLabelText('Sheet part for sheet-1.pdf');
    await waitFor(() => expect(optionLabels(rowSelect)).toHaveLength(3)); // (none) + 2 parts
    expect(optionLabels(rowSelect)).toEqual([
      '(none)',
      'SHT-304-125 — 304 SS 0.125 Sheet (12 EA on hand)',
      // No stock row => genuinely zero on hand (the summary only returns > 0).
      'SHT-AL-090 — AL 6061 0.090 Sheet (0 EA on hand)',
    ]);
    // "available" is quantity_allocated-dependent and that column is never
    // written — the hint must not claim it.
    expect(screen.queryByText(/available/i)).not.toBeInTheDocument();
  });

  it('never auto-ties a row from the AI-extracted material text', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    // Row 1's extracted material reads "304 SS" and a 304 sheet part exists —
    // it still must not be picked for the planner.
    expect(await screen.findByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue('');
  });

  it('the per-run input is disabled until the row is tied', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    const qty = screen.getByLabelText('Sheets per run for sheet-1.pdf');
    expect(qty).toBeDisabled();
    fireEvent.change(await screen.findByLabelText('Sheet part for sheet-1.pdf'), { target: { value: '31' } });
    expect(qty).toBeEnabled();
  });
});

describe('package-level default', () => {
  it('"Apply to all rows" stamps the pick onto every row and totals the deduction truthfully', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.change(await screen.findByLabelText('Sheet part'), { target: { value: '31' } });
    fireEvent.change(screen.getByLabelText('Sheets / run'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /apply to all rows/i }));

    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('31');
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue('31');
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toHaveValue(2);

    // 2/run x (5 + 2) runs = 14 sheets, deducted at WORK-ORDER completion —
    // never "deducting now" and never per run.
    const chip = screen.getByText(/sheets deducted when the work order finishes/i);
    expect(chip).toHaveTextContent('2 tied — 14 sheets deducted when the work order finishes');
    expect(screen.queryByText(/deducting now/i)).not.toBeInTheDocument();
  });

  it('leaves rows alone until the planner applies the package pick', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.change(await screen.findByLabelText('Sheet part'), { target: { value: '31' } });

    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
  });
});

describe('import payload', () => {
  it('sends the tie only on tied rows — an untied row is byte-identical to before', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.change(await screen.findByLabelText('Sheet part for sheet-1.pdf'), { target: { value: '31' } });
    fireEvent.change(screen.getByLabelText('Sheets per run for sheet-1.pdf'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).toEqual(
      expect.objectContaining({ source_file: 'sheet-1.pdf', material_part_id: 31, qty_per_run: 2 })
    );
    // Absent, not null-valued: an untied nest must not grow new keys.
    expect(payload.rows?.[1]).not.toHaveProperty('material_part_id');
    expect(payload.rows?.[1]).not.toHaveProperty('qty_per_run');
  });

  it('blocks the import when a tied row has a non-positive per-run quantity', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    fireEvent.change(await screen.findByLabelText('Sheet part for sheet-1.pdf'), { target: { value: '31' } });
    fireEvent.change(screen.getByLabelText('Sheets per run for sheet-1.pdf'), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    expect(await screen.findByText(/sheets per run for sheet-1\.pdf must be greater than 0/i)).toBeInTheDocument();
    expect(mockApi.importLaserNestPackage).not.toHaveBeenCalled();
  });
});

describe('re-import pre-fill', () => {
  it('pre-fills each row from the OPEN operation-scoped tie its nest already carries', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, part_id: 31, qty_per_run: 2 }),
      // Cancelled + work-order-scoped ties are not nest ties.
      tie({ id: 901, work_order_operation_id: 502, part_id: 32, status: 'cancelled' }),
      tie({ id: 902, work_order_operation_id: null, part_id: 32 }),
    ]);
    mockApi.getWorkOrder.mockResolvedValue({
      id: 42,
      operations: [
        { id: 501, laser_nest: { cnc_file_path: 'sheet-1.pdf' } },
        { id: 502, laser_nest: { cnc_file_path: 'sheet-2.pdf' } },
      ],
    });

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('31'));
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toHaveValue(2);
    // The cancelled tie on sheet-2 must NOT come back.
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue('');
    // The planner is told the re-import replaces what is there.
    expect(screen.getByText(/1 existing tie pre-filled/i)).toBeInTheDocument();
    // Live ties only.
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledWith(42, false);
  });

  it('degrades silently when the tie read fails and never blocks the review step', async () => {
    mockApi.getMaterialAllocations.mockRejectedValue(new Error('boom'));

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    expect(await screen.findByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
    expect(screen.queryByText(/existing tie/i)).not.toBeInTheDocument();
  });

  it('does not look for ties in standalone mode (there is no work order yet)', async () => {
    mockApi.previewLaserNestPackageStandalone.mockResolvedValue(preview);
    render(<LaserNestImportWizard open onClose={jest.fn()} onImported={jest.fn()} />);

    const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
    fireEvent.change(screen.getByLabelText(/zip package/i), { target: { files: [zip] } });
    fireEvent.click(screen.getByRole('button', { name: /^preview$/i }));
    await screen.findByRole('button', { name: /^import 2 nests$/i });

    expect(mockApi.getMaterialAllocations).not.toHaveBeenCalled();
  });
});

describe('consumed-tie refusal', () => {
  it('explains that nothing was destroyed and that there is no reversal verb yet', async () => {
    mockApi.importLaserNestPackage.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail:
            "Cannot rebuild this work order's operations: material has already been consumed " +
            'against 2 tied allocation(s). Reverse consumption first.',
        },
      },
    });

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    const banner = await screen.findByText(/material has already been consumed/i);
    // The server's own words survive verbatim...
    expect(banner).toHaveTextContent('Reverse consumption first.');
    // ...and the UI adds what the server can't say: nothing was lost, and the
    // only path today is a new work order (the RETURN verb ships in PR 3).
    expect(banner).toHaveTextContent(/nothing was imported and the existing nests are untouched/i);
    expect(banner).toHaveTextContent(/need a new work order/i);
  });

  it('renders a structured (object) 409 detail as text instead of crashing', async () => {
    mockApi.importLaserNestPackage.mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'TIE_CONSUMED', msg: 'Material already consumed.' } } },
    });

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    expect(await screen.findByText('Material already consumed.')).toBeInTheDocument();
  });
});

describe('degraded loads', () => {
  it('hides the package strip and disables the row pickers when no material parts load', async () => {
    mockApi.getMaterials.mockRejectedValue(new Error('materials down'));

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    expect(screen.queryByLabelText('Sheet part')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toBeDisabled();
  });

  it('shows no on-hand figure at all when the stock read fails (never a fabricated 0)', async () => {
    mockApi.getInventorySummary.mockRejectedValue(new Error('inventory down'));

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    const rowSelect = await screen.findByLabelText('Sheet part for sheet-1.pdf');
    await waitFor(() => expect(optionLabels(rowSelect)).toHaveLength(3));
    expect(optionLabels(rowSelect)).toEqual([
      '(none)',
      'SHT-304-125 — 304 SS 0.125 Sheet',
      'SHT-AL-090 — AL 6061 0.090 Sheet',
    ]);
    expect(within(rowSelect).queryByText(/on hand/i)).not.toBeInTheDocument();
  });
});
