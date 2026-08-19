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
 *  - the consumed-tie 409 renders the server's self-contained refusal verbatim
 *    (the old "no reversal verb yet" client addendum is retired — the RETURN
 *    verb shipped).
 *
 * Copy invariant: consumption fires when EACH OPERATION (one per nest)
 * completes, never per run — `utils/materialTie.ts` owns that copy.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import LaserNestImportWizard from './LaserNestImportWizard';
import { LaserNestPackagePreview, MaterialAllocation, Part } from '../../types';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import {
  comboBoxListbox,
  expectComboBoxOptions,
  openComboBox,
  selectComboBoxOption,
} from '../../test-utils/comboBox';

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
  // Automatic BOM backflush is off on every part until it is deliberately armed.
  backflush_components: false,
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

/** The label a committed pick shows in the picker's trigger. */
const SHEET_304_LABEL = 'SHT-304-125 — 304 SS 0.125 Sheet';

/**
 * The sheet-part picker for one nest row, once the material list has landed.
 *
 * The picker is a searchable combobox, not a `<select>`: it renders disabled
 * until `/materials` resolves, and its options only exist while the popup is
 * open. Waiting on `toBeEnabled` is what keeps these tests from opening an
 * empty list and asserting on nothing.
 */
async function rowPicker(sourceFile: string): Promise<HTMLElement> {
  const picker = await screen.findByLabelText(`Sheet part for ${sourceFile}`);
  await waitFor(() => expect(picker).toBeEnabled());
  return picker;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getWorkCenters.mockResolvedValue([]);
  mockApi.getMaterials.mockResolvedValue([SHEET_304, SHEET_AL]);
  mockApi.getInventorySummary.mockResolvedValue([{ part_id: 31, total_on_hand: 12 }]);
  mockApi.getMaterialAllocations.mockResolvedValue([]);
  mockApi.getWorkOrder.mockResolvedValue({ id: 42, operations: [] });
  mockApi.previewLaserNestPackage.mockResolvedValue(preview);
  mockApi.importLaserNestPackage.mockResolvedValue({
    child_work_order: { id: 909, work_order_number: 'WO-909' },
  });
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

    const picker = await rowPicker('sheet-1.pdf');
    openComboBox(picker);
    // (none) + 2 parts. Asserted by ACCESSIBLE NAME, which is label + on-hand
    // hint the way a screen reader announces it — the hint is its own element
    // now, not a suffix baked into one option string.
    expectComboBoxOptions(picker, [
      '(none — untied)',
      'SHT-304-125 — 304 SS 0.125 Sheet 12 EA on hand',
      // No stock row => genuinely zero on hand (the summary only returns > 0).
      'SHT-AL-090 — AL 6061 0.090 Sheet 0 EA on hand',
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
    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /SHT-304-125/);
    expect(qty).toBeEnabled();
  });
});

describe('package-level default', () => {
  it('"Apply to all rows" stamps the pick onto every row and totals the deduction truthfully', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    await rowPicker('sheet-1.pdf'); // materials loaded
    selectComboBoxOption(screen.getByLabelText('Sheet part'), /SHT-304-125/);
    fireEvent.change(screen.getByLabelText('Sheets / run'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /apply to all rows/i }));

    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(SHEET_304_LABEL);
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue(SHEET_304_LABEL);
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toHaveValue(2);

    // 2/run x (5 + 2) runs = 14 sheets, each nest's share deducted as ITS
    // operation completes — never "deducting now" and never per run.
    const chip = screen.getByText(/sheets deducted as each nest completes/i);
    expect(chip).toHaveTextContent('2 tied — 14 sheets deducted as each nest completes');
    expect(screen.queryByText(/deducting now/i)).not.toBeInTheDocument();
  });

  it('leaves rows alone until the planner applies the package pick', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    await rowPicker('sheet-1.pdf'); // materials loaded
    // A real commit on the package picker — not just typing in it — so the
    // assertion below is about the pick not leaking, not about nothing happening.
    selectComboBoxOption(screen.getByLabelText('Sheet part'), /SHT-304-125/);
    expect(screen.getByLabelText('Sheet part')).toHaveValue(SHEET_304_LABEL);

    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toBeDisabled();
  });
});

describe('import payload', () => {
  it('sends the tie only on tied rows — an untied row is byte-identical to before', async () => {
    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /SHT-304-125/);
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

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /SHT-304-125/);
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

    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(SHEET_304_LABEL));
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

/**
 * A tie whose part is one the shop PRODUCES cannot be carried across a
 * re-import, and the planner has to be told.
 *
 * The pre-fill is the one place a tie enters the wizard without anybody picking
 * it, so it is the one place a legacy manufactured/assembly tie can leak past
 * the pickers that now exclude them — straight into the import payload, and
 * from there into a completion that deletes finished goods from stock to build
 * themselves. `MaterialAllocation.part_type` is what makes the two
 * distinguishable at all (`part_number` / `part_name` look identical either
 * way).
 *
 * The refusal has three parts, and dropping any one of them re-opens the path:
 * the row is left untied, the part is NOT offered as a picker option (offering
 * it would make the bad tie one click from being re-committed), and a WARNING
 * toast says so — `success` would hide a shortfall the planner must act on, and
 * `error` would claim a failure that did not happen.
 */
describe('re-import pre-fill: a tie to a part the shop PRODUCES', () => {
  const renderWithToasts = () =>
    render(
      <ToastProvider>
        <LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />
      </ToastProvider>
    );

  /** One nest per source file, so every tie below has a row to land on. */
  const twoNestOperations = () =>
    mockApi.getWorkOrder.mockResolvedValue({
      id: 42,
      operations: [
        { id: 501, laser_nest: { cnc_file_path: 'sheet-1.pdf' } },
        { id: 502, laser_nest: { cnc_file_path: 'sheet-2.pdf' } },
      ],
    });

  const PRODUCED_TIE = {
    part_id: 77,
    part_number: 'BRK-100',
    part_name: 'Bracket, sheet metal',
    part_type: 'manufactured' as const,
  };

  it('leaves the row untied and warns, instead of pre-filling it', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();

    const warning = await screen.findByRole('alert');
    expect(warning).toHaveTextContent(
      '1 nest was tied to a part that is not stock material — re-pick the sheet before importing.'
    );
    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
    // Nothing was pre-filled, so there is no "N existing ties pre-filled" chip
    // claiming otherwise.
    expect(screen.queryByText(/existing tie/i)).not.toBeInTheDocument();
  });

  it('does not offer the produced part in the picker either', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    await screen.findByRole('alert');

    // Pinning it into the list — the mechanism that rightly keeps an unlisted
    // SHEET tie selectable — would make this bad tie re-selectable in one click.
    const picker = await rowPicker('sheet-1.pdf');
    openComboBox(picker);
    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /BRK-100/ })).toBeNull();
  });

  it('does not send the dropped tie on import', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    await screen.findByRole('alert');

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));
    // The import is now gated by the drop confirmation — see the describe below
    // for why. Nothing reaches the wire until it is answered.
    fireEvent.click(await screen.findByRole('button', { name: /^import untied$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).not.toHaveProperty('material_part_id');
  });

  it('carries the legitimate ties across and counts only the dropped one', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, part_id: 31, part_type: 'raw_material', qty_per_run: 2 }),
      tie({ id: 901, work_order_operation_id: 502, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();

    // One bad tie does not cost the planner the good ones.
    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(SHEET_304_LABEL));
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue('');
    expect(screen.getByText(/1 existing tie pre-filled/i)).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('1 nest was tied to a part that is not stock material');
  });

  it('pluralizes the warning over several dropped ties', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
      tie({ id: 901, work_order_operation_id: 502, part_id: 78, part_name: 'Weldment', part_type: 'assembly' }),
    ]);

    renderWithToasts();
    await previewPackage();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '2 nests were tied to a part that is not stock material — re-pick the sheet before importing.'
    );
  });

  it('pre-fills exactly as before when the server sends no part_type at all', async () => {
    // THE compatibility case. An older API omits `part_type` entirely; reading
    // absent as "suspect" would silently untie every nest on the work order the
    // moment the client ran ahead of the server. `tie()` omits the field, which
    // is precisely the older-server shape.
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, part_id: 31, qty_per_run: 2 }),
    ]);
    expect(tie({ id: 900 })).not.toHaveProperty('part_type');

    renderWithToasts();
    await previewPackage();

    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(SHEET_304_LABEL));
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toHaveValue(2);
    expect(screen.getByText(/1 existing tie pre-filled/i)).toBeInTheDocument();
    expect(screen.queryByText(/not stock material/i)).not.toBeInTheDocument();
  });

  it('says nothing when every tie is ordinary stock', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, part_id: 31, part_type: 'raw_material' }),
    ]);

    renderWithToasts();
    await previewPackage();

    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(SHEET_304_LABEL));
    expect(screen.queryByText(/not stock material/i)).not.toBeInTheDocument();
  });
});

/**
 * A dropped tie has to survive the toast, because the damage happens later.
 *
 * The toast that announces the drop lives about four seconds. Import is several
 * wizard steps and a grid-scroll after it, and pressing it makes the server
 * CANCEL the nest's live allocation while writing no replacement
 * (`build_laser_nest_child_work_order` -> `cancel_allocations_for_operations`).
 * The nest then runs with no material demand: no shortage is raised and stock is
 * never deducted when its operation completes — silently, and with no
 * auto-reversal (invariant 6b). A transient toast is not a surface for that.
 *
 * So the drop is carried on three durable surfaces, and each of them is what
 * the tests below pin:
 *   1. a notice on the AFFECTED ROW, beside the picker that fixes it;
 *   2. a chip in the summary strip, counting only what is still unresolved;
 *   3. a confirm on Import, so the loss is a decision that was read and taken.
 *
 * The manual-create path (`LaserNestManualModal`) already made exactly this
 * judgement for its single nest; this is the same rule at package scale.
 */
describe('re-import pre-fill: a dropped tie stays visible until it is dealt with', () => {
  const renderWithToasts = () =>
    render(
      <ToastProvider>
        <LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />
      </ToastProvider>
    );

  const twoNestOperations = () =>
    mockApi.getWorkOrder.mockResolvedValue({
      id: 42,
      operations: [
        { id: 501, laser_nest: { cnc_file_path: 'sheet-1.pdf' } },
        { id: 502, laser_nest: { cnc_file_path: 'sheet-2.pdf' } },
      ],
    });

  const PRODUCED_TIE = {
    part_id: 77,
    part_number: 'BRK-100',
    part_name: 'Bracket, sheet metal',
    part_type: 'manufactured' as const,
  };

  /** The Sheet part cell for one nest — where the row notice lives. */
  const sheetPartCell = (sourceFile: string): HTMLElement =>
    screen.getByLabelText(`Sheet part for ${sourceFile}`).closest('td') as HTMLElement;

  it('names the dropped part on the affected row, and leaves the others alone', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    await screen.findByRole('alert');

    // The notice is in the CELL, next to the picker that fixes it — not only in
    // a toast that has since expired.
    const affected = await within(sheetPartCell('sheet-1.pdf')).findByText(/BRK-100 — Bracket, sheet metal/);
    expect(affected).toHaveTextContent(/a part the shop produces rather than stock material/i);
    expect(affected).toHaveTextContent(/an untied nest never deducts stock/i);
    // ...and it is the picker's description, so a screen reader hears it with
    // the field rather than once, at load.
    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveAttribute('aria-describedby', affected.id);

    expect(within(sheetPartCell('sheet-2.pdf')).queryByText(/shop produces/i)).toBeNull();
  });

  it('surfaces a package whose ONLY ties are produced-part ties', async () => {
    // THE REGRESSION. The pre-fill used to return early once no tie survived to
    // be applied, which is exactly the package where EVERY tie was dropped — so
    // row state was never touched and those nests rendered indistinguishably
    // from ones that had never been tied at all.
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
      tie({ id: 901, work_order_operation_id: 502, part_id: 78, part_name: 'Weldment', part_type: 'assembly' }),
    ]);

    renderWithToasts();
    await previewPackage();
    await screen.findByRole('alert');

    expect(await within(sheetPartCell('sheet-1.pdf')).findByText(/BRK-100/)).toBeInTheDocument();
    expect(within(sheetPartCell('sheet-2.pdf')).getByText(/Weldment/)).toBeInTheDocument();
    expect(screen.getByText(/2 nests lost a material tie/i)).toBeInTheDocument();
  });

  it('clears the notice and the chip once the planner picks a real sheet', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    await within(sheetPartCell('sheet-1.pdf')).findByText(/BRK-100/);
    expect(screen.getByText(/1 nest lost a material tie/i)).toBeInTheDocument();

    const picker = await rowPicker('sheet-1.pdf');
    selectComboBoxOption(picker, /SHT-304-125/);

    // The nest carries a tie again, so there is nothing left to warn about —
    // a notice that outlived its cause is one planners learn to ignore.
    await waitFor(() => expect(within(sheetPartCell('sheet-1.pdf')).queryByText(/BRK-100/)).toBeNull());
    expect(screen.queryByText(/lost a material tie/i)).not.toBeInTheDocument();
  });

  it('asks before importing a nest whose tie it is about to drop', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    await within(sheetPartCell('sheet-1.pdf')).findByText(/BRK-100/);

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    // Named by NEST (its CNC number), because the remedy is per row. It also
    // has to say what the import DOES — "the tie is removed" reads like tidy-up.
    const confirm = await screen.findByText(/Import 1 nest with no material tie\?/i);
    expect(confirm).toBeInTheDocument();
    expect(screen.getByText(/8001 — was tied to BRK-100 — Bracket, sheet metal/)).toBeInTheDocument();
    expect(screen.getByText(/stock is never deducted when its operation completes/i)).toBeInTheDocument();
    // Nothing on the wire while the question is open.
    expect(mockApi.importLaserNestPackage).not.toHaveBeenCalled();
  });

  it('lets the planner back out of that import without losing the grid', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    await within(sheetPartCell('sheet-1.pdf')).findByText(/BRK-100/);

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));
    fireEvent.click(await screen.findByRole('button', { name: /go back and pick/i }));

    await waitFor(() => expect(screen.queryByText(/Import 1 nest with no material tie\?/i)).toBeNull());
    expect(mockApi.importLaserNestPackage).not.toHaveBeenCalled();
    // Still on the review grid, still flagged, still fixable.
    expect(within(sheetPartCell('sheet-1.pdf')).getByText(/BRK-100/)).toBeInTheDocument();
  });

  it('does not ask at all once every dropped tie has been re-picked', async () => {
    // The confirm is about UNRESOLVED drops. Asking about a nest the planner has
    // already fixed is the nag that teaches people to click through it.
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, ...PRODUCED_TIE }),
    ]);

    renderWithToasts();
    await previewPackage();
    const picker = await rowPicker('sheet-1.pdf');
    await within(sheetPartCell('sheet-1.pdf')).findByText(/BRK-100/);
    selectComboBoxOption(picker, /SHT-304-125/);
    await waitFor(() => expect(within(sheetPartCell('sheet-1.pdf')).queryByText(/BRK-100/)).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/with no material tie\?/i)).toBeNull();
    // The re-pick is what actually goes to the server: a real sheet part, tied.
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).toMatchObject({ material_part_id: 31 });
  });

  it('never asks when the package carried no dropped tie', async () => {
    twoNestOperations();
    mockApi.getMaterialAllocations.mockResolvedValue([
      tie({ id: 900, work_order_operation_id: 501, part_id: 31, part_type: 'raw_material' }),
    ]);

    renderWithToasts();
    await previewPackage();
    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(SHEET_304_LABEL));

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/with no material tie\?/i)).toBeNull();
  });
});

describe('consumed-tie refusal', () => {
  it('renders the ledger-backed 409 refusal verbatim, with no client addendum', async () => {
    // The current backend wording (material_consumption_service): self-contained
    // — it says what stands on the ledger, that returning the material does not
    // unlock the rebuild, and that the remedy is a new work order.
    const detail =
      "Cannot rebuild this work order's operations: this work order's material movement is " +
      'already on the inventory ledger for 2 tied allocation(s), and the rebuild would delete ' +
      'the operations those ledger rows are recorded against — dropping them out of job cost, ' +
      'analytics and lot traceability. Returning the material does not change that. Raise a ' +
      'new work order for the corrected nest package; this one keeps its material history intact.';
    mockApi.importLaserNestPackage.mockRejectedValue({
      response: { status: 409, data: { detail } },
    });

    render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);
    await previewPackage();
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    const banner = await screen.findByText(/already on the inventory ledger/i);
    expect(banner).toHaveTextContent(detail);
    // The retired client addendum claimed reversal "is not available yet" —
    // false since returnMaterialAllocation shipped. It must never come back.
    expect(banner).not.toHaveTextContent(/not available yet/i);
    expect(banner).not.toHaveTextContent(/nothing was imported/i);
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

    const picker = await rowPicker('sheet-1.pdf');
    openComboBox(picker);
    expectComboBoxOptions(picker, [
      '(none — untied)',
      'SHT-304-125 — 304 SS 0.125 Sheet',
      'SHT-AL-090 — AL 6061 0.090 Sheet',
    ]);
    expect(within(comboBoxListbox(picker)).queryByText(/on hand/i)).not.toBeInTheDocument();
  });
});
