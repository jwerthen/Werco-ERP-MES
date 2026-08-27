/**
 * WorkOrderTemplatesPanel — a template whose source job was deleted is a WORKING
 * template. This file exists because the owner hit the opposite behaviour:
 * "Templates need to stay even if there is no work order present for it."
 *
 * The neighbouring suites pin the note's wording and its Deleted-tab pointer. This
 * one pins the thing the owner actually cares about — that the row is not treated
 * as broken — and it pins it END TO END, through the Use dialog the panel mounts,
 * because that is where the refusal used to land.
 *
 * Why it can never be more than a soft delete: `source_work_order_id` is NOT NULL
 * with no `ON DELETE`, so the source row physically cannot be gone while the
 * template exists. A soft-deleted work order keeps every operation, nest, tie and
 * process-sheet step it had, so the server reads the plan straight through it —
 * `available` stays true, the counts are real — and reports the deletion as the
 * purely informational `plan.source_work_order_deleted`.
 *
 * Five properties, each of which a refactor can undo silently:
 *
 * 1. **No error treatment on the row.** Asserted on the TREATMENT — no `role=alert`,
 *    no `text-fd-red`, no `opacity-70` dimming, no `template-unavailable-*` line —
 *    not on the sentence, because the regression that matters keeps the words and
 *    restores the red.
 *
 * 2. **The note reads as context.** Muted class, no alert role, no instruction.
 *
 * 3. **Use works, IDENTICALLY to a live-source template.** Asserted as parity: the
 *    same flow is driven against a live-source row in the same list and the two
 *    payloads are compared, so "it submits something" cannot pass for "it submits
 *    the same thing".
 *
 * 4. **A genuinely unavailable template is still refused** — including a token this
 *    build has never seen, since the server owns that vocabulary and it is OPEN.
 *    Widening the deletion carve-out into "nothing is ever unavailable" is the
 *    over-correction this catches.
 *
 * 5. **The plan summary renders real counts**, not the em-dash a `!available` row
 *    gets. A planner picks a template off those counts.
 *
 * The panel passes no `mobileCards`, so DataTable mounts one row per template and
 * single queries are safe. Wrapped in a MemoryRouter because the restorer branch of
 * the note renders a react-router `<Link>`.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import WorkOrderTemplatesPanel from './WorkOrderTemplatesPanel';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { WorkOrderDuplicateResult, WorkOrderTemplate, WorkOrderTemplatePlan } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listWorkOrderTemplates: jest.fn(),
    updateWorkOrderTemplate: jest.fn(),
    deleteWorkOrderTemplate: jest.fn(),
    useWorkOrderTemplate: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const makePlan = (overrides: Partial<WorkOrderTemplatePlan> = {}): WorkOrderTemplatePlan => ({
  available: true,
  unavailable_reason: null,
  source_work_order_number: 'WO-20260501-004',
  source_status: 'complete',
  work_order_type: 'production',
  sequential_operations: true,
  priority: 3,
  operation_count: 4,
  nest_count: 0,
  planned_runs_total: 0,
  open_material_tie_count: 0,
  work_centers: ['BRAKE-2', 'WELD-1'],
  source_quantity_ordered: 50,
  ...overrides,
});

const makeTemplate = (overrides: Partial<WorkOrderTemplate> = {}): WorkOrderTemplate => ({
  id: 7,
  name: 'Bracket brake set',
  notes: null,
  source_work_order_id: 42,
  default_quantity: 12,
  created_at: '2026-08-20T12:00:00Z',
  updated_at: '2026-08-20T12:00:00Z',
  created_by: 3,
  plan: makePlan(),
  ...overrides,
});

/** The control: an ordinary template pointing at a live work order. */
const LIVE_TEMPLATE = makeTemplate();

/**
 * The subject. Everything below is populated exactly as for a live source — the
 * server read the plan THROUGH the soft-deleted work order — and the counts are
 * deliberately non-zero and non-default so a blanked plan cell is visible.
 *
 * It is deliberately NOT nest-bearing, and matches `LIVE_TEMPLATE`'s
 * `default_quantity`: those two inputs are what decide the payload (a nest-bearing
 * template's quantity is derived server-side, so the field locks and nothing is
 * sent), and the parity test below is worthless unless the only difference between
 * the two rows is the deletion itself.
 */
const DELETED_SOURCE_TEMPLATE = makeTemplate({
  id: 9,
  name: 'Old weld fixture',
  default_quantity: 12,
  plan: makePlan({
    source_work_order_deleted: true,
    source_work_order_number: 'WO-20260420-007',
    operation_count: 6,
    nest_count: 0,
    planned_runs_total: 0,
    open_material_tie_count: 2,
    work_centers: ['LASER-1', 'WELD-1'],
  }),
});

/**
 * Genuinely unresolvable, for a reason this build has never seen. The vocabulary is
 * the server's and OPEN, so an unknown token must still refuse — a client that only
 * knows how to refuse the tokens it recognizes silently offers a write the server
 * will 409.
 */
const UNKNOWN_REASON_TEMPLATE = makeTemplate({
  id: 11,
  name: 'Mystery template',
  plan: makePlan({
    available: false,
    unavailable_reason: 'source_company_closed',
    source_work_order_number: null,
    source_status: null,
    operation_count: 0,
    work_centers: [],
    source_quantity_ordered: null,
  }),
});

/** Unresolvable with no reason at all — the server may send none. */
const REASONLESS_TEMPLATE = makeTemplate({
  id: 12,
  name: 'Reasonless template',
  plan: makePlan({ available: false, unavailable_reason: null }),
});

const ENVELOPE: WorkOrderDuplicateResult = {
  work_order: {
    id: 501,
    version: 1,
    work_order_number: 'WO-20260825-002',
    part_id: 10,
    work_order_type: 'production',
    quantity_ordered: 12,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'draft',
    priority: 3,
    estimated_hours: 0,
    actual_hours: 0,
    created_at: '2026-08-25T12:00:00Z',
    updated_at: '2026-08-25T12:00:00Z',
    operations: [],
  },
  skipped_operations: [],
  skipped_material_allocations: [],
};

const list = (templates: WorkOrderTemplate[]) => ({ templates, total: templates.length });

function renderPanel(props: { canRestoreWorkOrders?: boolean } = {}) {
  const onUsed = jest.fn();
  const utils = render(
    <ToastProvider>
      <MemoryRouter>
        <WorkOrderTemplatesPanel onUsed={onUsed} {...props} />
      </MemoryRouter>
    </ToastProvider>
  );
  return { ...utils, onUsed };
}

/** The desktop row a template renders into. */
const rowFor = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

/** Columns: Template | Kind | Source WO | Plan | Work centers | Saved | actions. */
const planCell = (name: string) => within(rowFor(name)).getAllByRole('cell')[3];

/**
 * Drive the whole Use flow for one template and return the payload the client sent.
 * Used for both rows so the deleted-source path is compared against the live one
 * rather than merely asserted to be non-empty.
 */
async function useTemplate(name: string) {
  await userEvent.click(screen.getByRole('button', { name: `Use template ${name}` }));
  await userEvent.click(await screen.findByRole('button', { name: /Create draft work order/i }));
  await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalled());
  return mockApi.useWorkOrderTemplate.mock.calls[0];
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.listWorkOrderTemplates.mockResolvedValue(list([LIVE_TEMPLATE, DELETED_SOURCE_TEMPLATE]));
  mockApi.useWorkOrderTemplate.mockResolvedValue(ENVELOPE);
});

describe('WorkOrderTemplatesPanel: a deleted source job carries NO error treatment', () => {
  it('leaves the row undimmed and free of every refusal marker', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');
    const row = rowFor('Old weld fixture');

    // Assert the TREATMENT, not the words: the regression that matters keeps the
    // sentence and puts the red back. `opacity-70` is the dimming DataTable applies
    // via `rowClassName` to an unavailable row, and it must not reach this one.
    expect(row.className).not.toMatch(/opacity-70/);
    expect(within(row).queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByTestId('template-unavailable-9')).not.toBeInTheDocument();
    expect(row.querySelector('.text-fd-red')).toBeNull();
  });

  it('leaves Use enabled and untitled — a disabled-reason tooltip is a refusal too', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    const use = screen.getByRole('button', { name: 'Use template Old weld fixture' });
    expect(use).toBeEnabled();
    // The panel puts the unavailable sentence on `title` when it disables Use.
    // A leftover title would whisper "cannot be used" under a working button.
    expect(use).not.toHaveAttribute('title');
  });

  it('renders the note in the muted secondary colour, with no alert role', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    const note = screen.getByTestId('template-source-deleted-9');
    expect(note.className).toMatch(/text-surface-500/);
    expect(note.className).not.toMatch(/text-fd-red|font-medium/);
    expect(note).not.toHaveAttribute('role');
    // Context, not an instruction: there is nothing here for the planner to fix,
    // and any "restore it first" wording would send them off to do exactly that.
    expect(note).not.toHaveTextContent(/must|restore it (first|before)|cannot/i);
  });
});

describe('WorkOrderTemplatesPanel: Use behaves identically to a live-source template', () => {
  it('sends the SAME payload shape, against the deleted-source template id', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    const [templateId, payload] = await useTemplate('Old weld fixture');

    expect(templateId).toBe(9);
    expect(payload).toEqual({ quantity_ordered: 12, due_date: null });
  });

  it('matches the live-source payload byte for byte — parity, not merely "it submits"', async () => {
    // Asserting only that a call happened would pass for a client that silently
    // dropped the quantity or fabricated a due date on this path.
    renderPanel();
    await screen.findByText('Old weld fixture');
    const [, deletedPayload] = await useTemplate('Old weld fixture');

    jest.clearAllMocks();
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([LIVE_TEMPLATE, DELETED_SOURCE_TEMPLATE]));
    mockApi.useWorkOrderTemplate.mockResolvedValue(ENVELOPE);
    renderPanel();
    await screen.findAllByText('Bracket brake set');
    const [, livePayload] = await useTemplate('Bracket brake set');

    expect(deletedPayload).toEqual(livePayload);
  });

  it('hands the WHOLE envelope to onUsed, exactly as a live source does', async () => {
    const { onUsed } = renderPanel();
    await screen.findByText('Old weld fixture');

    await useTemplate('Old weld fixture');

    await waitFor(() => expect(onUsed).toHaveBeenCalledWith(ENVELOPE));
  });

  it('opens the dialog with the form, not a refusal box', async () => {
    // The refusal used to land HERE, one click after a row that already looked
    // broken — so the panel-level assertions above are only half the property.
    renderPanel();
    await screen.findByText('Old weld fixture');

    await userEvent.click(screen.getByRole('button', { name: 'Use template Old weld fixture' }));

    expect(await screen.findByRole('button', { name: /Create draft work order/i })).toBeEnabled();
    expect(screen.queryByTestId('use-template-unavailable')).not.toBeInTheDocument();
    // The dialog discloses the deletion too, muted — the planner should not learn
    // it afterwards from a work order number that leads nowhere.
    expect(screen.getByTestId('use-template-source-deleted')).not.toHaveAttribute('role', 'alert');
  });
});

describe('WorkOrderTemplatesPanel: a genuinely unavailable template is still refused', () => {
  it('renders an UNKNOWN reason token verbatim, in the red treatment, with Use disabled', async () => {
    // The over-correction this catches: widening "a deleted source is fine" into
    // "nothing is ever unavailable" would offer a write the server refuses 409.
    mockApi.listWorkOrderTemplates.mockResolvedValue(
      list([DELETED_SOURCE_TEMPLATE, UNKNOWN_REASON_TEMPLATE])
    );
    renderPanel();
    await screen.findByText('Mystery template');

    const line = screen.getByTestId('template-unavailable-11');
    expect(line).toHaveTextContent('source_company_closed');
    expect(line.className).toMatch(/text-fd-red/);
    expect(rowFor('Mystery template').className).toMatch(/opacity-70/);

    const use = screen.getByRole('button', { name: 'Use template Mystery template' });
    expect(use).toBeDisabled();
    expect(use).toHaveAttribute('title', expect.stringContaining('source_company_closed'));
    // And the deleted-source row beside it is untouched by that refusal.
    expect(screen.getByRole('button', { name: 'Use template Old weld fixture' })).toBeEnabled();
  });

  it('refuses a reasonless unavailable template rather than falling through to usable', async () => {
    // `unavailable_reason: null` is a shape the server may send. A client that keys
    // usability off the REASON instead of off `available` would enable Use here.
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([REASONLESS_TEMPLATE]));
    renderPanel();
    await screen.findByText('Reasonless template');

    expect(screen.getByTestId('template-unavailable-12')).toHaveTextContent(
      'This template cannot be used right now.'
    );
    expect(screen.getByRole('button', { name: 'Use template Reasonless template' })).toBeDisabled();
  });

  it('carries no deleted-source note onto a row that is unavailable for another reason', async () => {
    // The note hangs off `source_work_order_deleted`, never off `!available` —
    // pointing a planner at the archive for a cause the archive cannot fix is a
    // wrong instruction, not a harmless extra.
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([UNKNOWN_REASON_TEMPLATE]));
    renderPanel({ canRestoreWorkOrders: true });
    await screen.findByText('Mystery template');

    expect(screen.queryByTestId('template-source-deleted-11')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Deleted tab/i })).not.toBeInTheDocument();
  });
});

describe('WorkOrderTemplatesPanel: the plan summary survives the deletion', () => {
  it('renders the real counts, not the em-dash an unavailable row gets', async () => {
    // A planner picks a template off these numbers. Blanking them for a deleted
    // source would make every archived-source template look empty.
    renderPanel();
    await screen.findByText('Old weld fixture');

    const cell = planCell('Old weld fixture');
    expect(cell).toHaveTextContent('6 ops · 2 open ties');
    expect(cell).not.toHaveTextContent('—');
    expect(rowFor('Old weld fixture')).toHaveTextContent('LASER-1 → WELD-1');
  });

  it('still shows the source work order number and its status badge', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    const row = rowFor('Old weld fixture');
    expect(row).toHaveTextContent('WO-20260420-007');
    // A deleted job keeps the status it had; a blanked one would read as a row the
    // server could not resolve, which is the conclusion this whole change reverses.
    expect(row).not.toHaveTextContent('#42');
  });

  it('blanks the plan cell ONLY for a row the server could not resolve', async () => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(
      list([DELETED_SOURCE_TEMPLATE, UNKNOWN_REASON_TEMPLATE])
    );
    renderPanel();
    await screen.findByText('Mystery template');

    expect(planCell('Mystery template')).toHaveTextContent('—');
    expect(planCell('Old weld fixture')).toHaveTextContent('6 ops');
  });
});
