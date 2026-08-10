/**
 * LaserNestImportWizard — server-computed sheet-part suggestions.
 *
 * The wizard now opens a previewed package with each nest's sheet part already
 * proposed, instead of 42 empty comboboxes. What this file guards is the line
 * between a PROPOSAL and a TIE, because the tie is what makes stock leave
 * inventory when the nest's operation completes, into an as-built record that
 * never auto-reverses:
 *
 *  - a suggestion pre-fills the picker and NOTHING else. It is not counted in
 *    the "N tied" chip, its per-run input stays disabled, the thickness /
 *    sheet-size pull-through does not fire, and it cannot reach the wire;
 *  - one deliberate human act commits the batch, and the confirmation rolls the
 *    package up BY DISTINCT PART — the review that makes a wrong sheet visible;
 *  - both entry points (the Accept button, and pressing Import with suggestions
 *    outstanding) land on that same dialog. Import does not bounce the planner
 *    with an error toast: the suggestions are the expected state of a fresh
 *    package, and one click is the whole point;
 *  - accepting is mechanically identical to a bulk explicit pick, so an accepted
 *    row pulls its spec through exactly like a hand-picked one;
 *  - precedence is server tie > planner pick > suggestion.
 *
 * The wizard renders a server-computed field; it never guesses. Fixtures without
 * a `sheet_suggestion` must behave exactly as they did before this feature —
 * that regression is pinned in LaserNestImportWizard.materialTie.test.tsx
 * ("never auto-ties a row from the AI-extracted material text"), which is why
 * nothing here relies on the AI-read `material` text.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import LaserNestImportWizard from './LaserNestImportWizard';
import {
  LaserNestPackagePreview,
  MaterialAllocation,
  Part,
  SheetPartCandidate,
  SheetPartSuggestion,
} from '../../types';
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

// Real shapes from the shop's stocking convention, so the spec pull-through is
// observable on accept (see utils/sheetPart.ts).
const PLATE_188 = part({ id: 41, part_number: '0.188-72X144-A36', name: 'A36 HR plate' });
const SHEET_10GA = part({ id: 42, part_number: '10GA-72X120-CS', name: 'CS sheet' });
const PLATE_250 = part({ id: 43, part_number: '0.250-60X120-A36', name: 'A36 plate' });

const PLATE_188_LABEL = '0.188-72X144-A36 — A36 HR plate';
const SHEET_10GA_LABEL = '10GA-72X120-CS — CS sheet';
const PLATE_250_LABEL = '0.250-60X120-A36 — A36 plate';

const candidate = (overrides: Partial<SheetPartCandidate> = {}): SheetPartCandidate => ({
  part_id: PLATE_188.id,
  part_number: PLATE_188.part_number,
  part_name: PLATE_188.name,
  unit_of_measure: 'EA',
  score: 100,
  on_hand: 40,
  on_hand_known: true,
  demand: 3,
  projected_on_hand: 37,
  stock_state: 'covered',
  spec_thickness: '0.188',
  spec_sheet_size: '72x144',
  is_sheet_like: true,
  prior_tie_count: 6,
  reason: 'Exact thickness and A36 on a 72x144 sheet.',
  basis: 'deterministic',
  diagnostics: [],
  ...overrides,
});

/** A confident single-part match — the shape that pre-fills a picker. */
const matched = (overrides: Partial<SheetPartCandidate> = {}): SheetPartSuggestion => {
  const only = candidate(overrides);
  return { status: 'matched', auto_fill_part_id: only.part_id, candidates: [only], diagnostic: null };
};

/** Two survivors and no winner: a shortlist, and nothing pre-filled. */
const ambiguous = (candidates: SheetPartCandidate[], diagnostic: string): SheetPartSuggestion => ({
  status: 'ambiguous',
  auto_fill_part_id: null,
  candidates,
  diagnostic,
});

interface NestFixture {
  file: string;
  runs?: number;
  thickness?: string;
  size?: string;
  suggestion?: SheetPartSuggestion | null;
}

function previewOf(rows: NestFixture[]): LaserNestPackagePreview {
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
      sheet_suggestion: row.suggestion ?? null,
    })),
  };
}

/** Pick a ZIP, run Preview, and wait for the review grid + the loaded pickers. */
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
const perRunOf = (file: string) => screen.getByLabelText(`Sheets per run for ${file}`);

/**
 * The accept-confirmation dialog.
 *
 * Both it and the wizard are `role="dialog"` portaled to `document.body`, and
 * both carry a "Cancel" button, so every assertion inside it is scoped by
 * finding the dialog that owns the rollup rather than by picking a button by
 * name off the whole screen.
 */
function confirmDialog(): HTMLElement {
  const found = screen.getAllByRole('dialog').find((el) => /suggested sheet tie/i.test(el.textContent ?? ''));
  if (!found) throw new Error('the accept-suggestions dialog is not open');
  return found;
}

const renderWizard = () =>
  render(<LaserNestImportWizard open workOrderId={42} onClose={jest.fn()} onImported={jest.fn()} />);

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getWorkCenters.mockResolvedValue([]);
  mockApi.getMaterials.mockResolvedValue([PLATE_188, SHEET_10GA, PLATE_250]);
  mockApi.getInventorySummary.mockResolvedValue([{ part_id: 41, total_on_hand: 40 }]);
  mockApi.getMaterialAllocations.mockResolvedValue([]);
  mockApi.getWorkOrder.mockResolvedValue({ id: 42, operations: [] });
  mockApi.importLaserNestPackage.mockResolvedValue({
    child_work_order: { id: 909, work_order_number: 'WO-909' },
  });
});

describe('a matched suggestion is a proposal, not a tie', () => {
  beforeEach(() => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', runs: 3, suggestion: matched() }, { file: 'sheet-2.pdf', runs: 2 }])
    );
  });

  it('pre-fills the picker without counting as tied, stamping a spec, or reaching the wire', async () => {
    renderWizard();
    await previewPackage();

    // Pre-filled: the planner is looking at the answer, not at 500 options.
    expect(await rowPicker('sheet-1.pdf')).toHaveValue(PLATE_188_LABEL);
    // A row the server said nothing about is untouched.
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue('');

    // …but nothing downstream of a TIE has happened. Per-run is enabled by a
    // confirmed tie only.
    expect(perRunOf('sheet-1.pdf')).toBeDisabled();
    // The chip that claims stock will move must not count a proposal.
    expect(screen.queryByText(/sheets deducted as each nest completes/i)).not.toBeInTheDocument();
    // The pull-through does NOT fire at suggestion time: overwriting the
    // extractor's read on the strength of a second machine guess is not
    // evidence, it is two models agreeing.
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.125"');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('48x96');
    expect(thicknessOf('sheet-1.pdf')).not.toHaveAttribute('title', expect.stringContaining('From '));

    // The state is said out loud, and never as an accomplishment.
    expect(screen.getByText('1 sheet matched — confirm before importing')).toBeInTheDocument();
  });

  it('cannot be imported without a confirmation — cancelling sends nothing at all', async () => {
    renderWizard();
    await previewPackage();
    await rowPicker('sheet-1.pdf');

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    // No error toast, no dead end: the expected path opens the rollup.
    const dialog = await waitFor(confirmDialog);
    fireEvent.click(within(dialog).getByRole('button', { name: /^cancel$/i }));

    await waitFor(() => expect(screen.queryAllByRole('dialog')).toHaveLength(1));
    expect(mockApi.importLaserNestPackage).not.toHaveBeenCalled();
    // Still a proposal afterwards — cancelling is not a rejection of the match.
    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(PLATE_188_LABEL);
    expect(perRunOf('sheet-1.pdf')).toBeDisabled();
  });

  it('warns when a suggested sheet will not cover the package', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([
        { file: 'sheet-1.pdf', runs: 3, suggestion: matched({ on_hand: 0, projected_on_hand: -3, stock_state: 'none' }) },
        { file: 'sheet-2.pdf', runs: 2 },
      ])
    );
    renderWizard();
    await previewPackage();
    await rowPicker('sheet-1.pdf');

    expect(screen.getByText('1 suggested sheet is short on stock')).toBeInTheDocument();
  });
});

describe('accepting the batch', () => {
  const THREE_NESTS = previewOf([
    { file: 'sheet-1.pdf', runs: 3, suggestion: matched() },
    { file: 'sheet-2.pdf', runs: 2, suggestion: matched() },
    {
      file: 'sheet-3.pdf',
      runs: 4,
      suggestion: matched({
        part_id: SHEET_10GA.id,
        part_number: SHEET_10GA.part_number,
        part_name: SHEET_10GA.name,
        on_hand: 0,
        projected_on_hand: -4,
        stock_state: 'none',
        spec_thickness: '10 ga',
        spec_sheet_size: '72x120',
      }),
    },
  ]);

  it('rolls the package up BY DISTINCT PART, so a wrong sheet is one short line', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(THREE_NESTS);
    renderWizard();
    await previewPackage(3);
    await rowPicker('sheet-1.pdf');

    fireEvent.click(screen.getByRole('button', { name: 'Accept 3 suggested' }));
    const dialog = await waitFor(confirmDialog);

    // Three rows collapse to two lines. The count leads each line, which is what
    // makes an odd sheet — a line reading "1 x …" — visible at a glance.
    expect(dialog).toHaveTextContent('Accept 3 suggested sheet ties?');
    expect(dialog).toHaveTextContent('2 x 0.188-72X144-A36 — 5 sheets');
    // The short line states its stock; a covered line does not need a number.
    expect(dialog).toHaveTextContent('1 x 10GA-72X120-CS — 4 sheets — 0 EA on hand');
    expect(dialog).not.toHaveTextContent('2 x 0.188-72X144-A36 — 5 sheets — ');
    // The consequence, in the feature's one canonical phrasing.
    expect(dialog).toHaveTextContent('Material leaves stock as each nest operation completes.');
  });

  it('confirming ties every suggested row and pulls each part’s spec through', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(THREE_NESTS);
    renderWizard();
    await previewPackage(3);
    await rowPicker('sheet-1.pdf');

    fireEvent.click(screen.getByRole('button', { name: 'Accept 3 suggested' }));
    fireEvent.click(within(await waitFor(confirmDialog)).getByRole('button', { name: /^accept ties$/i }));

    // Now they are ties: counted, deducting, and per-run editable.
    await waitFor(() =>
      expect(screen.getByText(/sheets deducted as each nest completes/i)).toHaveTextContent(
        '3 tied — 9 sheets deducted as each nest completes'
      )
    );
    expect(perRunOf('sheet-1.pdf')).toBeEnabled();
    // Accepting IS a bulk pick, so the spec pull-through fires exactly as it
    // does for a hand-picked part — including on the gauge form.
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.188');
    expect(sheetSizeOf('sheet-1.pdf')).toHaveValue('72x144');
    expect(thicknessOf('sheet-3.pdf')).toHaveValue('10 ga');
    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining(PLATE_188_LABEL));

    // Nothing is outstanding any more.
    expect(screen.queryByText(/matched — confirm before importing/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /accept \d+ suggested/i })).not.toBeInTheDocument();
  });
});

describe('Import with suggestions outstanding', () => {
  beforeEach(() => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', runs: 3, suggestion: matched() }, { file: 'sheet-2.pdf', runs: 2 }])
    );
  });

  it('opens the same dialog and, on confirm, accepts and imports in one click', async () => {
    renderWizard();
    await previewPackage();
    await rowPicker('sheet-1.pdf');

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));
    const dialog = await waitFor(confirmDialog);
    fireEvent.click(within(dialog).getByRole('button', { name: /^accept & import$/i }));

    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    // The accepted row carries a real tie…
    expect(payload.rows?.[0]).toEqual(
      expect.objectContaining({ source_file: 'sheet-1.pdf', material_part_id: 41, qty_per_run: 1 })
    );
    // …and the row nobody tied is byte-identical to a pre-feature import.
    expect(payload.rows?.[1]).not.toHaveProperty('material_part_id');
    expect(payload.rows?.[1]).not.toHaveProperty('qty_per_run');
    // Provenance rides alongside and records the fact this breadcrumb exists to
    // answer: the tie was the machine's proposal, confirmed by a human ('auto'),
    // as distinct from one the planner chose themselves ('planner').
    //
    // The untied row is OMITTED, not sent as a fifth value: no tie was
    // committed, so there is no decision to describe, and an invented entry in
    // an append-only audit row is worse than an absent one.
    expect(payload.sheet_match_provenance).toEqual({ 'sheet-1.pdf': 'auto' });
  });

  it('surfaces a bad row before the rollup, not after the planner has confirmed it', async () => {
    renderWizard();
    await previewPackage();
    await rowPicker('sheet-1.pdf');

    fireEvent.change(screen.getByLabelText('CNC number for sheet-1.pdf'), { target: { value: '  ' } });
    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    expect(await screen.findByText(/enter a cnc number for sheet-1\.pdf/i)).toBeInTheDocument();
    expect(screen.queryAllByRole('dialog')).toHaveLength(1);
    expect(mockApi.importLaserNestPackage).not.toHaveBeenCalled();
  });
});

describe('an ambiguous row', () => {
  const DIAGNOSTIC = 'Two 0.125" stainless sheets match — the nest report does not say which.';
  const SHORTLIST = ambiguous(
    [
      candidate({ score: 85, reason: 'Exact thickness; alloy under-specified.' }),
      candidate({
        part_id: 99,
        part_number: 'SHT-OLD-1',
        part_name: 'Retired 0.075 sheet',
        score: 84,
        on_hand: 7,
        on_hand_known: true,
        reason: 'Exact thickness; alloy under-specified.',
      }),
    ],
    DIAGNOSTIC
  );

  beforeEach(() => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', runs: 3, suggestion: SHORTLIST }, { file: 'sheet-2.pdf', runs: 2 }])
    );
  });

  it('leaves the picker empty and seeds the shortlist into it — pick from 2, not 500', async () => {
    renderWizard();
    await previewPackage();

    const picker = await rowPicker('sheet-1.pdf');
    // Nothing is pre-filled when the data does not identify ONE sheet.
    expect(picker).toHaveValue('');
    expect(perRunOf('sheet-1.pdf')).toBeDisabled();
    expect(screen.queryByText(/matched — confirm before importing/i)).not.toBeInTheDocument();

    // The shortlisted part the material list never returned is selectable
    // anyway, with its stock stated the way every other option states it.
    openComboBox(picker);
    const option = within(comboBoxListbox(picker)).getByRole('option', { name: /SHT-OLD-1/ });
    expect(option).toHaveAccessibleName('SHT-OLD-1 — Retired 0.075 sheet 7 EA on hand');

    // Why it is a shortlist and not an answer.
    expect(screen.getByTitle(DIAGNOSTIC)).toBeInTheDocument();
  });

  it('picking from the shortlist is an ordinary pick — tied, spec pulled through, serialized', async () => {
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);

    expect(perRunOf('sheet-1.pdf')).toBeEnabled();
    expect(thicknessOf('sheet-1.pdf')).toHaveValue('0.188');

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    // No suggestion is outstanding, so Import goes straight through.
    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).toEqual(expect.objectContaining({ material_part_id: 41, qty_per_run: 1 }));
    // Chosen from the shortlist by hand, so it records as 'planner' — NOT
    // 'auto'. A part the planner picked because the machine could not decide is
    // not the machine's suggestion, and conflating them would make the
    // "are the suggestions any good?" question unanswerable.
    expect(payload.sheet_match_provenance).toEqual({ 'sheet-1.pdf': 'planner' });
  });
});

describe('clearing a suggestion', () => {
  it('goes back to untied, and imports with no tie keys on the row', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', runs: 3, suggestion: matched() }, { file: 'sheet-2.pdf', runs: 2 }])
    );
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), '(none — untied)');

    expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue('');
    expect(perRunOf('sheet-1.pdf')).toBeDisabled();
    expect(screen.queryByText(/matched — confirm before importing/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^import 2 nests$/i }));

    // Nothing outstanding: no dialog, straight to the wire, no tie keys.
    await waitFor(() => expect(mockApi.importLaserNestPackage).toHaveBeenCalledTimes(1));
    const [, payload] = mockApi.importLaserNestPackage.mock.calls[0];
    expect(payload.rows?.[0]).not.toHaveProperty('material_part_id');
    // Nothing was tied, so nothing is described: an entirely untied package
    // sends an EMPTY provenance map, not a map full of "none".
    expect(payload.sheet_match_provenance).toEqual({});
  });
});

describe('precedence: server tie > planner pick > suggestion', () => {
  const tie = (overrides: Partial<MaterialAllocation>): MaterialAllocation => ({
    id: 900,
    work_order_id: 42,
    work_order_operation_id: 501,
    operation_number: '10',
    detached_from_operation_id: null,
    part_id: PLATE_250.id,
    part_number: PLATE_250.part_number,
    part_name: PLATE_250.name,
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

  it('a persisted tie replaces a suggestion, but never a pick made while it was in flight', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([
        { file: 'sheet-1.pdf', runs: 3, suggestion: matched() },
        { file: 'sheet-2.pdf', runs: 2, suggestion: matched() },
      ])
    );
    // The tie read is deliberately held open: the pre-fill runs un-awaited
    // behind an already-interactive grid, and the race it has to survive is the
    // planner picking during exactly that window.
    let releaseTies: (ties: MaterialAllocation[]) => void = () => undefined;
    mockApi.getMaterialAllocations.mockImplementation(
      () =>
        new Promise<MaterialAllocation[]>((resolve) => {
          releaseTies = resolve;
        })
    );
    mockApi.getWorkOrder.mockResolvedValue({
      id: 42,
      operations: [
        { id: 501, laser_nest: { cnc_file_path: 'sheet-1.pdf' } },
        { id: 502, laser_nest: { cnc_file_path: 'sheet-2.pdf' } },
      ],
    });

    renderWizard();
    await previewPackage();

    // Both rows opened on the same suggestion; the planner corrects row 2.
    expect(await rowPicker('sheet-1.pdf')).toHaveValue(PLATE_188_LABEL);
    selectComboBoxOption(await rowPicker('sheet-2.pdf'), /10GA-72X120-CS/);

    releaseTies([
      tie({ id: 900, work_order_operation_id: 501 }),
      tie({ id: 901, work_order_operation_id: 502 }),
    ]);

    // Row 1 was only a suggestion — a human already committed the stored tie, so
    // the stored tie wins, and it arrives as a real tie (per-run editable).
    await waitFor(() => expect(screen.getByLabelText('Sheet part for sheet-1.pdf')).toHaveValue(PLATE_250_LABEL));
    expect(perRunOf('sheet-1.pdf')).toBeEnabled();
    expect(perRunOf('sheet-1.pdf')).toHaveValue(2);

    // Row 2 was a deliberate choice made seconds ago. Overwriting it would
    // import a part nobody chose, with nothing marked and nothing to restore.
    expect(screen.getByLabelText('Sheet part for sheet-2.pdf')).toHaveValue(SHEET_10GA_LABEL);
    expect(screen.queryByText(/matched — confirm before importing/i)).not.toBeInTheDocument();
  });
});

describe('the “part disagrees with the nest report” marker', () => {
  it('compares thickness numerically, so 0.1875 and 0.188 are not a divergence', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([
        { file: 'sheet-1.pdf', thickness: '0.1875', size: '72x144' },
        { file: 'sheet-2.pdf', thickness: '0.125"', size: '48x96' },
      ])
    );
    renderWizard();
    await previewPackage();

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);
    selectComboBoxOption(await rowPicker('sheet-2.pdf'), /0\.188-72X144-A36/);

    // Same sheet, written two ways: sourced, but NOT flagged. A string compare
    // lights this on every row of a 42-nest package and trains the planner to
    // ignore the one marker that matters.
    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', `From ${PLATE_188_LABEL}.`);
    // A real disagreement still says what it displaced.
    expect(thicknessOf('sheet-2.pdf')).toHaveAttribute('title', expect.stringContaining('read as "0.125""'));
  });

  it('still flags a sheet-size disagreement, which has no tolerance semantics', async () => {
    mockApi.previewLaserNestPackage.mockResolvedValue(
      previewOf([{ file: 'sheet-1.pdf', thickness: '0.188', size: '60x120' }])
    );
    renderWizard();
    await previewPackage(1);

    selectComboBoxOption(await rowPicker('sheet-1.pdf'), /0\.188-72X144-A36/);

    expect(thicknessOf('sheet-1.pdf')).toHaveAttribute('title', `From ${PLATE_188_LABEL}.`);
    expect(sheetSizeOf('sheet-1.pdf')).toHaveAttribute('title', expect.stringContaining('read as "60x120"'));
  });
});
