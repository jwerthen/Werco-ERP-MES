/**
 * LaserNestImportWizard — the sheet-part spec pull-through, and the run-count
 * "not read" chip.
 *
 * Two behaviors that both exist because an AI read of a nest report is weaker
 * evidence than a human naming a stock item:
 *
 *  1. PULL-THROUGH. Picking a sheet part stamps that part's real thickness and
 *     sheet size onto the nest row. The rules that make an uncommanded write
 *     acceptable are what this file pins: it happens only for parts the
 *     sheet-stock heuristic accepts, it is marked and it says what it displaced,
 *     a hand-typed value ends it, and untying puts the extractor's value back
 *     ONLY where the planner has not typed since. Thickness is what an operator
 *     loads a machine from — a second silent write over a corrected value is the
 *     failure this design is built against.
 *
 *  2. THE RUN-COUNT CHIP. `planned_runs` arrives floored at 1 by the server, so
 *     "runs once" and "no run count was found" are the same number on the wire.
 *     `field_confidence.planned_runs === 'low'` is the only thing separating
 *     them, and the chip is how a 42-nest package stops presenting 42
 *     confident-looking 1s. It has to clear per row as the planner fixes each
 *     one, or it is just noise.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import LaserNestImportWizard from './LaserNestImportWizard';
import { LaserNestFieldConfidence, LaserNestPackagePreview, MaterialAllocation, Part } from '../../types';
import api from '../../services/api';
import { comboBoxListbox, openComboBox, selectComboBoxOption } from '../../test-utils/comboBox';

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
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

// Real shapes from the shop's stocking convention: the number states thickness
// then width x length. See utils/sheetPart.ts.
const PLATE_188 = part({ id: 41, part_number: '0.188-72X144-A36', name: 'A36 HR plate' });
const SHEET_10GA = part({ id: 42, part_number: '10GA-72X120-CS', name: 'CS sheet' });
// Angle: NOT sheet-like, so it is behind the picker's "show all materials"
// escape hatch — and nothing may be derived from it even once it is picked.
const ANGLE = part({ id: 43, part_number: 'ANG-A36-1.5X1.5X.25', name: 'A36 Angle 1.5 x 1.5 x .25' });

const PLATE_188_LABEL = '0.188-72X144-A36 — A36 HR plate';
const ANGLE_LABEL = 'ANG-A36-1.5X1.5X.25 — A36 Angle 1.5 x 1.5 x .25';

function previewOf(
  rows: Array<{ file: string; runs?: number; thickness?: string; size?: string; fc?: LaserNestFieldConfidence | null }>
): LaserNestPackagePreview {
  return {
    package_name: 'nests.zip',
    nest_count: rows.length,
    total_planned_runs: rows.reduce((sum, r) => sum + (r.runs ?? 1), 0),
    nests: rows.map((row, i) => ({
      source_file: row.file,
      nest_name: `Sheet ${i + 1}`,
      cnc_number: `800${i + 1}`,
      cnc_file_name: null,
      planned_runs: row.runs ?? 1,
      material: 'A36',
      thickness: row.thickness ?? '0.125"',
      sheet_size: row.size ?? '48x96',
      confidence: 'high',
      field_confidence: row.fc ?? null,
    })),
  };
}

/** The two-nest package the pull-through tests use. */
const TWO_NESTS = previewOf([
  { file: 'sheet-1.pdf', runs: 3, thickness: '0.125"', size: '48x96' },
  { file: 'sheet-2.pdf', runs: 2, thickness: '0.135"', size: '60x120' },
]);

async function previewPackage(nestCount = 2) {
  const zip = new File(['PK'], 'nests.zip', { type: 'application/zip' });
  fireEvent.change(screen.getByLabelText(/zip package/i), { target: { files: [zip] } });
  fireEvent.click(screen.getByRole('button', { name: /^preview$/i }));
  await screen.findByRole('button', {
    name: new RegExp(`^import ${nestCount} ${nestCount === 1 ? 'nest' : 'nests'}$`, 'i'),
  });
  await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());
}

/** A row's sheet-part picker, once the material list has landed. */
async function rowPicker(sourceFile: string): Promise<HTMLElement> {
  const picker = await screen.findByLabelText(`Sheet part for ${sourceFile}`);
  await waitFor(() => expect(picker).toBeEnabled());
  return picker;
}

const thicknessOf = (file: string) => screen.getByLabelText(new RegExp(`^Thickness for ${file.replace('.', '\\.')}`));
const sheetSizeOf = (file: string) => screen.getByLabelText(new RegExp(`^Sheet size for ${file.replace('.', '\\.')}`));
const runsOf = (file: string) => screen.getByLabelText(new RegExp(`^Runs for ${file.replace('.', '\\.')}`));

const renderWizard = () =>
  render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getWorkCenters.mockResolvedValue([]);
  mockApi.getMaterials.mockResolvedValue([PLATE_188, SHEET_10GA, ANGLE]);
  mockApi.getInventorySummary.mockResolvedValue([]);
  mockApi.getMaterialAllocations.mockResolvedValue([]);
  mockApi.getWorkOrder.mockResolvedValue({ id: 42, operations: [] });
  mockApi.previewLaserNestPackage.mockResolvedValue(TWO_NESTS);
  mockApi.importLaserNestPackage.mockResolvedValue({
    child_work_order: { id: 909, work_order_number: 'WO-909' },
  });
});

describe('picking a sheet part pulls its spec through', () => {
  it('fills thickness and sheet size on that row, and only that row', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);

    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.188');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('72x144');
    // A per-row pick is a per-row statement.
    expect(thicknessOf('sheet-2.pdf')).toHaveValue('0.135"');
    expect(sheetSizeOf('sheet-2.pdf')).toHaveValue('60x120');
  });

  it('says where the value came from, and what it displaced', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);

    // A divergence from a non-empty extractor read is usually the signal that
    // the wrong part was tied — the write is never silent.
    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining(PLATE_188_LABEL));
    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining('read as "0.125""'));
  });

  it('derives from the gauge form as well as the decimal form', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /10GA-72X120-CS/);

    expect(thicknessOf('sheet-1.pdf')).toHaveValue('10 ga');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('72x120');
  });

  it('stamps NOTHING for a part that is not sheet stock', async () => {
    renderWizard();
    await previewPackage();

    const picker = await rowPicker('sheet-1.pdf');
    openComboBox(picker);
    // Angle only exists behind the escape hatch — the default filter is what
    // keeps it out of a sheet picker in the first place.
    expect(within(comboBoxListbox(picker)).queryByRole('option', { name: /ANG-A36/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show all materials (1 more)' }));
    fireEvent.click(within(comboBoxListbox(picker)).getByRole('option', { name: /ANG-A36/ }));

    // The tie itself is honored — the planner asked for it.
    expect(picker).toHaveValue(ANGLE_LABEL);
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toBeEnabled();
    // But its part number's `1.5X1.5X.25` must never become a sheet spec.
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.125"');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('48x96');
    expect(thicknessOf('sheet-1.pdf')).not.toHaveAttribute('title', expect.stringContaining('From '));
  });

  it('leaves the extractor’s value alone when the part states no dimensions', async () => {
    mockApi.getMaterials.mockResolvedValue([part({ id: 44, part_number: 'SHT-304-125', name: '304 SS 0.125 Sheet' })]);
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /SHT-304-125/);

    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.125"');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('48x96');
  });
});

describe('"Apply to all rows"', () => {
  it('stamps the spec onto every row', async () => {
    renderWizard();
    await previewPackage();
    await rowPicker('sheet-1.pdf'); // materials loaded

    selectComboBoxOption(screen.getByLabelText('Sheet part'), /0\.188-72X144-A36/);
    fireEvent.click(screen.getByRole('button', { name: /apply to all rows/i }));

    for (const file of ['sheet-1.pdf', 'sheet-2.pdf']) {
      expect(screen.getByLabelText(`Sheet part for ${file}`)).toHaveValue(PLATE_188_LABEL);
      expect(thicknessOf(file)).toHaveValue('0.188');
      expect(sheetSizeOf(file)).toHaveValue('72x144');
    }
  });

  it('re-stamps over a previous pull-through without stranding the original read', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    selectComboBoxOption(screen.getByLabelText('Sheet part'), /10GA-72X120-CS/);
    fireEvent.click(screen.getByRole('button', { name: /apply to all rows/i }));

    expect(thicknessOf('sheet-1.pdf')).toHaveValue('10 ga');
    // The displaced value tracked for a restore is still the EXTRACTOR's read,
    // not the intermediate derived one — otherwise untying would leave the row
    // holding a spec from a part it is no longer tied to.
    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining('read as "0.125""'));

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), '(none — untied)');
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.125"');
  });
});

describe('a hand-typed spec is the planner’s, permanently', () => {
  it('sticks, and stops being marked as derived', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.188');

    fireEvent.change(thicknessOf('sheet-1.pdf'), { target: { value: '0.1875' } });

    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.1875');
    expect(thicknessOf('sheet-1.pdf')).not.toHaveAttribute('title', expect.stringContaining('From '));
    // Only the field the planner touched loses its marking.
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('72x144');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining(PLATE_188_LABEL));
  });

  it('is not clobbered when the tie is cleared afterwards', async () => {
    renderWizard();
    await previewPackage();

    const picker = await rowPicker('sheet-1.pdf');
    selectComboBoxOption(picker, /0\.188-72X144-A36/);
    fireEvent.change(thicknessOf('sheet-1.pdf'), { target: { value: '0.1875' } });

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), '(none — untied)');

    // Restoring "0.125"" here would be a SECOND uncommanded write, over a value
    // a human just typed. That is the failure the marking exists to prevent.
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.1875');
  });

  it('is not clobbered when a different part is tied afterwards either', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    fireEvent.change(thicknessOf('sheet-1.pdf'), { target: { value: '0.1875' } });

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /10GA-72X120-CS/);

    // Re-tying is an explicit act, so the new part's spec does win — but it must
    // not first restore the extractor's read over the planner's edit, and the
    // recorded displaced value is now the planner's, not "0.125"".
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('10 ga');
    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining('read as "0.1875"'));
  });
});

describe('clearing the tie', () => {
  it('restores the extractor’s value on untouched fields', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.188');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('72x144');

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), '(none — untied)');

    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.125"');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('48x96');
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toBeDisabled();
  });

  it('restores field by field — untouched back, edited left alone', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    // Only the thickness is corrected by hand; the sheet size is left as the
    // part supplied it.
    fireEvent.change(thicknessOf('sheet-1.pdf'), { target: { value: '0.1875' } });

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), '(none — untied)');

    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.1875');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('48x96');
  });

  it('sends the restored values on import, with no tie keys on the row', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    selectComboBoxOption(await rowPicker('sheet-1.pdf'), '(none — untied)');
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).toEqual(
      expect.objectContaining({ source_file: 'sheet-1.pdf', thickness: '0.125"', sheet_size: '48x96' })
    );
    expect(payload.rows?.[0]).not.toHaveProperty('material_part_id');
  });

  it('sends the derived values on import when the tie stands', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).toEqual(
      expect.objectContaining({
        source_file: 'sheet-1.pdf',
        thickness: '0.188',
        sheet_size: '72x144',
        material_part_id: 41,
      })
    );
  });
});

describe('a pre-filled tie survives the sheet-stock filter', () => {
  const tie = (overrides: Partial<MaterialAllocation>): MaterialAllocation => ({
    id: 900,
    work_order_id: 42,
    work_order_operation_id: 501,
    operation_number: '10',
    detached_from_operation_id: null,
    part_id: 41,
    part_number: PLATE_188.part_number,
    part_name: PLATE_188.name,
    source: 'nest',
    status: 'open',
    qty_per_run: 2,
    qty_planned: 6,
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

  /** One nest already tied to `allocation`'s part on a re-import. */
  function arrangeExistingTie(allocation: MaterialAllocation) {
    mockApi.getMaterialAllocations.mockResolvedValue([allocation]);
    mockApi.getWorkOrder.mockResolvedValue({
      id: 42,
      operations: [{ id: 501, laser_nest: { cnc_file_path: 'sheet-1.pdf' } }],
    });
  }

  it('shows a tie to NON-sheet stock rather than rendering blank', async () => {
    // The planner tied this nest to angle through the "show all" escape hatch.
    // The default filter would hide it — and a blank picker on a re-import is
    // exactly how a work order gets quietly untied, because the import CANCELS
    // and DETACHES every existing tie as it rebuilds the operations.
    arrangeExistingTie(tie({ part_id: 43, part_number: ANGLE.part_number, part_name: ANGLE.name }));
    renderWizard();
    await previewPackage();

    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(ANGLE_LABEL));
    expect(screen.getByLabelText('Sheets per run for sheet-1.pdf')).toHaveValue(2);
    // Still no spec pulled through — pinning it into the list is not endorsing
    // its dimensions.
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.125"');
  });

  it('shows a tie to a part the material list never returned', async () => {
    // Capped list, deactivated part, or a failed /materials read.
    arrangeExistingTie(tie({ part_id: 99, part_number: 'SHT-OLD-1', part_name: 'Retired 0.075 sheet' }));
    renderWizard();
    await previewPackage();

    await waitFor(() =>
      expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('SHT-OLD-1 — Retired 0.075 sheet')
    );

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    // The tie rides through the re-import intact — that is the whole point of
    // keeping an unlisted part selectable.
    expect(payload.rows?.[0]).toEqual(
      expect.objectContaining({ source_file: 'sheet-1.pdf', material_part_id: 99, qty_per_run: 2 })
    );
  });

  it('keeps a pinned selection reachable in the list, not just in the trigger', async () => {
    arrangeExistingTie(tie({ part_id: 43, part_number: ANGLE.part_number, part_name: ANGLE.name }));
    renderWizard();
    await previewPackage();

    const picker = await rowPicker('sheet-1.pdf');
    await waitFor(() => expect(picker).toHaveValue(ANGLE_LABEL));
    openComboBox(picker);

    // Filtered out by default for everyone else, present for the row that is
    // tied to it — and marked as the committed option.
    const option = within(comboBoxListbox(picker)).getByRole('option', { name: ANGLE_LABEL });
    expect(option).toHaveAttribute('aria-selected', 'true');
  });
});

describe('the "run counts not read" chip', () => {
  const CHIP = /run counts? not read — defaulted to 1/i;

  it('does not appear when every run count was read', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([
        { file: 'sheet-1.pdf', runs: 3, fc: { planned_runs: 'high' } },
        { file: 'sheet-2.pdf', runs: 1, fc: { planned_runs: 'medium' } },
      ])
    );
    renderWizard();
    await previewPackage();

    expect(screen.queryByText(CHIP)).not.toBeInTheDocument();
  });

  it('does not appear when the package carries no per-field confidence at all', async () => {
    // ZIP/CNC-program packages have no field_confidence — absent is not "low".
    renderWizard();
    await previewPackage();

    expect(screen.queryByText(CHIP)).not.toBeInTheDocument();
  });

  it('counts exactly the rows whose run count could not be read', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([
        { file: 'sheet-1.pdf', runs: 1, fc: { planned_runs: 'low' } },
        { file: 'sheet-2.pdf', runs: 1, fc: { planned_runs: 'low' } },
        { file: 'sheet-3.pdf', runs: 4, fc: { planned_runs: 'high' } },
      ])
    );
    renderWizard();
    await previewPackage(3);

    // The two floored 1s are called out; the read 4 is not. Without this, a
    // package of defaulted 1s is indistinguishable from a package that runs once
    // per nest.
    expect(screen.getByText('2 run counts not read — defaulted to 1')).toBeInTheDocument();
  });

  it('clears one row at a time as the planner fixes each count', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([
        { file: 'sheet-1.pdf', runs: 1, fc: { planned_runs: 'low' } },
        { file: 'sheet-2.pdf', runs: 1, fc: { planned_runs: 'low' } },
      ])
    );
    renderWizard();
    await previewPackage();

    expect(screen.getByText('2 run counts not read — defaulted to 1')).toBeInTheDocument();

    fireEvent.change(runsOf('sheet-1.pdf'), { target: { value: '6' } });
    expect(screen.getByText('1 run count not read — defaulted to 1')).toBeInTheDocument();

    fireEvent.change(runsOf('sheet-2.pdf'), { target: { value: '2' } });
    expect(screen.queryByText(CHIP)).not.toBeInTheDocument();
  });

  it('keeps flagging a row whose defaulted 1 turns out to be right', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', runs: 1, fc: { planned_runs: 'low' } }])
    );
    renderWizard();
    await previewPackage(1);

    expect(screen.getByText('1 run count not read — defaulted to 1')).toBeInTheDocument();

    // Re-entering the same 1 is not an edit — React's controlled input fires no
    // change for an unchanged value, so `edited.planned_runs` never gets set.
    // KNOWN LIMITATION, pinned deliberately: the flag is derived from the VALUE
    // changing, not from the planner acknowledging it, so a nest that genuinely
    // runs once stays chipped through import. Harmless (the chip says
    // "defaulted to 1" and the planner has confirmed it is 1) but it is why
    // there is no "verified" affordance to test for.
    fireEvent.change(runsOf('sheet-1.pdf'), { target: { value: '1' } });

    expect(screen.getByText('1 run count not read — defaulted to 1')).toBeInTheDocument();

    // Changing it — in either direction — is what clears it.
    fireEvent.change(runsOf('sheet-1.pdf'), { target: { value: '2' } });
    expect(screen.queryByText(CHIP)).not.toBeInTheDocument();
  });

  it('names the row too, so the planner knows which sheet to check', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', runs: 1, fc: { planned_runs: 'low' } }])
    );
    renderWizard();
    await previewPackage(1);

    expect(runsOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining('No run count was found'));
  });
});
