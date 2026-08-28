/**
 * WorkOrderTemplatesPanel — the catalog of jobs the shop re-runs.
 *
 * Four properties here are the reason the file exists:
 *
 * 1. **A deleted source job does not break a template.** The source work order can
 *    only ever be SOFT-deleted (NOT NULL FK, no `ON DELETE`), and a soft-deleted
 *    work order keeps its whole plan — so the server reads through it, the counts
 *    are real and Use stays ENABLED. The row carries a muted note, never the red
 *    refusal it used to.
 *
 * 2. **An unusable template is FLAGGED, never hidden.** A source the server
 *    genuinely cannot resolve still comes back `available = false` with a reason.
 *    Filtering it out is the mask trap invariant 3 documents: the row would vanish
 *    and every fix starts with seeing it. So the row renders, carries the reason in
 *    words, and its Use action is DISABLED to match the server's 409.
 *
 * 3. **The plan summary is read live and shown honestly.** Nothing about the plan
 *    is stored on the template row; a nest count on a picker card is only worth
 *    rendering because the server recomputes it off the live source work order.
 *
 * 4. **The three async states are the shared primitives, and Retry actually
 *    re-fetches.** A failed load must not be a blank section a planner reads as
 *    "no templates yet" — the two are opposite conclusions.
 *
 * NOTE: DataTable renders the desktop table AND (when given `mobileCards`) the
 * mobile cards; CSS hides one per breakpoint and jsdom applies no breakpoints.
 * This panel passes no `mobileCards`, so single queries are safe here — but keep
 * that in mind if one is added.
 *
 * Mocks `services/api`; the wire shapes are pinned in
 * `services/api.workOrderTemplates.test.ts`.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import WorkOrderTemplatesPanel from './WorkOrderTemplatesPanel';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { WorkOrderTemplate, WorkOrderTemplatePlan } from '../../types';

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

const NEST_TEMPLATE = makeTemplate({
  id: 8,
  name: 'Miratech nest group',
  notes: 'Runs on the Ermaksan.',
  default_quantity: null,
  plan: makePlan({
    source_work_order_number: 'WO-20260601-011',
    source_status: 'in_progress',
    work_order_type: 'laser_cutting',
    // false = a same-work-center dispatch POOL, which the card has to disclose.
    sequential_operations: false,
    operation_count: 21,
    nest_count: 21,
    planned_runs_total: 63,
    open_material_tie_count: 2,
    work_centers: ['LASER-1'],
    source_quantity_ordered: 63,
  }),
});

/**
 * A template whose source work order was SOFT-DELETED — still fully usable. The
 * plan is read through the deleted work order, so every count is real.
 */
const DELETED_SOURCE_TEMPLATE = makeTemplate({
  id: 9,
  name: 'Old weld fixture',
  plan: makePlan({
    source_work_order_deleted: true,
    source_work_order_number: 'WO-20260420-007',
    operation_count: 4,
    work_centers: ['WELD-1'],
  }),
});

/** A template the server genuinely cannot resolve — the only unusable shape left. */
const DEAD_TEMPLATE = makeTemplate({
  id: 11,
  name: 'Mystery template',
  plan: makePlan({
    available: false,
    unavailable_reason: 'some_future_reason',
    source_work_order_number: null,
    source_status: null,
    work_order_type: null,
    sequential_operations: null,
    priority: null,
    operation_count: 0,
    open_material_tie_count: 0,
    work_centers: [],
    source_quantity_ordered: null,
  }),
});

const list = (templates: WorkOrderTemplate[]) => ({ templates, total: templates.length });

function renderPanel() {
  const onUsed = jest.fn();
  const utils = render(
    <ToastProvider>
      <WorkOrderTemplatesPanel onUsed={onUsed} />
    </ToastProvider>
  );
  return { ...utils, onUsed };
}

/** The row a template renders into (desktop table). */
const rowFor = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate(), NEST_TEMPLATE]));
});

describe('WorkOrderTemplatesPanel: the catalog', () => {
  it('renders a row per template with its name, note and source work order', async () => {
    renderPanel();

    expect(await screen.findByText('Bracket brake set')).toBeInTheDocument();
    expect(screen.getByText('Miratech nest group')).toBeInTheDocument();
    expect(screen.getByText('Runs on the Ermaksan.')).toBeInTheDocument();
    expect(screen.getByText('WO-20260501-004')).toBeInTheDocument();
    expect(screen.getByText('WO-20260601-011')).toBeInTheDocument();
  });

  it('reads the envelope, not a bare array', async () => {
    renderPanel();
    await screen.findByText('Bracket brake set');
    expect(mockApi.listWorkOrderTemplates).toHaveBeenCalledWith(undefined);
  });

  it('badges a nest-bearing template as a nest group and discloses the dispatch pool', async () => {
    renderPanel();
    await screen.findByText('Miratech nest group');

    const nestRow = rowFor('Miratech nest group');
    expect(within(nestRow).getByText('Nest group')).toBeInTheDocument();
    // `sequential_operations === false` is a same-work-center dispatch pool, and
    // the copy carries the setting — so it belongs on the card a planner picks from.
    expect(within(nestRow).getByText('Pool')).toBeInTheDocument();

    const plainRow = rowFor('Bracket brake set');
    expect(within(plainRow).getByText('Production')).toBeInTheDocument();
    expect(within(plainRow).queryByText('Pool')).not.toBeInTheDocument();
  });

  it('summarizes the LIVE plan — ops, nests, runs and open ties', async () => {
    renderPanel();
    await screen.findByText('Miratech nest group');

    const nestRow = rowFor('Miratech nest group');
    expect(nestRow).toHaveTextContent('21 ops · 21 nests · 63 runs · 2 open ties');
    expect(nestRow).toHaveTextContent('LASER-1');

    // Zero counts are omitted rather than rendered as noise.
    expect(rowFor('Bracket brake set')).toHaveTextContent('4 ops');
    expect(rowFor('Bracket brake set')).toHaveTextContent('BRAKE-2 → WELD-1');
  });
});

describe('WorkOrderTemplatesPanel: a deleted source job is context, not a broken template', () => {
  beforeEach(() => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate(), DELETED_SOURCE_TEMPLATE]));
  });

  it('says so in a MUTED note, not the red refusal treatment', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    const note = screen.getByTestId('template-source-deleted-9');
    expect(note).toHaveTextContent(/source work order was deleted — the saved plan still copies/i);
    // The red class is the error treatment; this is context and must not borrow it.
    expect(note.className).not.toMatch(/text-fd-red/);
    // And it is not the unavailable line — that branch is for a source the server
    // genuinely cannot resolve, which a soft-deleted work order is not.
    expect(screen.queryByTestId('template-unavailable-9')).not.toBeInTheDocument();
  });

  it('leaves Use ENABLED — the plan is read through the deleted work order', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    expect(screen.getByRole('button', { name: 'Use template Old weld fixture' })).toBeEnabled();
  });

  it('still summarizes the plan, because the counts are real', async () => {
    renderPanel();
    await screen.findByText('Old weld fixture');

    expect(rowFor('Old weld fixture')).toHaveTextContent('4 ops');
    expect(rowFor('Old weld fixture')).toHaveTextContent('WELD-1');
  });
});

describe('WorkOrderTemplatesPanel: an unusable template is flagged, not hidden', () => {
  beforeEach(() => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate(), DEAD_TEMPLATE]));
  });

  it('still renders the row and names the cause in words', async () => {
    renderPanel();

    expect(await screen.findByText('Mystery template')).toBeInTheDocument();
    // The vocabulary is the server's and is OPEN: an unrecognized token gets its own
    // sentence, verbatim, rather than being dropped or guessed at.
    expect(screen.getByTestId('template-unavailable-11')).toHaveTextContent('some_future_reason');
  });

  it('disables Use on that row, and only that row', async () => {
    renderPanel();
    await screen.findByText('Mystery template');

    expect(screen.getByRole('button', { name: 'Use template Mystery template' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Use template Bracket brake set' })).toBeEnabled();
  });

  it('leaves Rename and Delete available — deleting it is one of the fixes', async () => {
    renderPanel();
    await screen.findByText('Mystery template');

    expect(screen.getByRole('button', { name: 'Rename template Mystery template' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Delete template Mystery template' })).toBeEnabled();
  });
});

describe('WorkOrderTemplatesPanel: the three async states', () => {
  it('renders an empty state that tells the planner how to make one', async () => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([]));
    renderPanel();

    expect(await screen.findByText('No work order templates yet')).toBeInTheDocument();
    expect(screen.getByText(/Save as template/i)).toBeInTheDocument();
  });

  it('renders an error state, NOT an empty one, when the load fails', async () => {
    // The two read as opposite conclusions: "no templates yet" would send a
    // planner off to create one that already exists.
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockApi.listWorkOrderTemplates.mockRejectedValue(new Error('boom'));
    renderPanel();

    expect(await screen.findByRole('button', { name: /Retry/i })).toBeInTheDocument();
    expect(screen.queryByText('No work order templates yet')).not.toBeInTheDocument();
    consoleError.mockRestore();
  });

  it('Retry actually re-fetches and recovers', async () => {
    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
    mockApi.listWorkOrderTemplates.mockRejectedValueOnce(new Error('boom'));
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate()]));
    renderPanel();

    await userEvent.click(await screen.findByRole('button', { name: /Retry/i }));

    expect(await screen.findByText('Bracket brake set')).toBeInTheDocument();
    expect(mockApi.listWorkOrderTemplates).toHaveBeenCalledTimes(2);
    consoleError.mockRestore();
  });
});

describe('WorkOrderTemplatesPanel: rename and delete are server-gated', () => {
  it('renames through the shared input dialog and re-reads the catalog', async () => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate()]));
    mockApi.updateWorkOrderTemplate.mockResolvedValue(makeTemplate({ name: 'Brake set — rev B' }));
    renderPanel();
    await screen.findByText('Bracket brake set');

    await userEvent.click(screen.getByRole('button', { name: 'Rename template Bracket brake set' }));
    const field = await screen.findByLabelText(/Template name/i);
    await userEvent.clear(field);
    await userEvent.type(field, 'Brake set — rev B');
    await userEvent.click(screen.getByRole('button', { name: 'Rename' }));

    await waitFor(() =>
      expect(mockApi.updateWorkOrderTemplate).toHaveBeenCalledWith(7, { name: 'Brake set — rev B' })
    );
    // Non-optimistic: the row comes back from the server, not from local state.
    await waitFor(() => expect(mockApi.listWorkOrderTemplates).toHaveBeenCalledTimes(2));
  });

  it('keeps the rename dialog OPEN when the server refuses the new name', async () => {
    // Names are unique among live templates, compared case-insensitively — a 409
    // this panel cannot predict. Closing over it would lose the typing.
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate()]));
    mockApi.updateWorkOrderTemplate.mockRejectedValue({
      response: { status: 409, data: { detail: 'A work order template with that name already exists.' } },
    });
    renderPanel();
    await screen.findByText('Bracket brake set');

    await userEvent.click(screen.getByRole('button', { name: 'Rename template Bracket brake set' }));
    const field = await screen.findByLabelText(/Template name/i);
    await userEvent.clear(field);
    await userEvent.type(field, 'Miratech nest group');
    await userEvent.click(screen.getByRole('button', { name: 'Rename' }));

    expect(
      await screen.findByText('A work order template with that name already exists.')
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Template name/i)).toHaveValue('Miratech nest group');
  });

  it('confirms before deleting, and says what a delete does NOT touch', async () => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate()]));
    mockApi.deleteWorkOrderTemplate.mockResolvedValue({ message: 'deleted', id: 7 });
    renderPanel();
    await screen.findByText('Bracket brake set');

    await userEvent.click(screen.getByRole('button', { name: 'Delete template Bracket brake set' }));
    // A template holds nothing that cannot be re-created in one click, and the
    // drafts it already produced are ordinary work orders. Say so before the click.
    expect(await screen.findByText(/every draft it has already created, are untouched/i)).toBeInTheDocument();
    expect(mockApi.deleteWorkOrderTemplate).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(mockApi.deleteWorkOrderTemplate).toHaveBeenCalledWith(7));
    await waitFor(() => expect(mockApi.listWorkOrderTemplates).toHaveBeenCalledTimes(2));
  });
});

describe('WorkOrderTemplatesPanel: using a template hands off rather than navigating itself', () => {
  it('opens the Use dialog and reports the created draft to the caller', async () => {
    mockApi.listWorkOrderTemplates.mockResolvedValue(list([makeTemplate()]));
    const envelope = {
      work_order: {
        id: 501,
        version: 1,
        work_order_number: 'WO-20260825-002',
        part_id: 10,
        work_order_type: 'production' as const,
        quantity_ordered: 12,
        quantity_complete: 0,
        quantity_scrapped: 0,
        status: 'draft' as const,
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
    mockApi.useWorkOrderTemplate.mockResolvedValue(envelope);
    const { onUsed } = renderPanel();
    await screen.findByText('Bracket brake set');

    await userEvent.click(screen.getByRole('button', { name: 'Use template Bracket brake set' }));
    await userEvent.click(await screen.findByRole('button', { name: /Create draft work order/i }));

    await waitFor(() => expect(onUsed).toHaveBeenCalledWith(envelope));
  });
});
