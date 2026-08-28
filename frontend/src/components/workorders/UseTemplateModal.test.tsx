/**
 * UseTemplateModal — run a saved plan again as a new DRAFT.
 *
 * This dialog is the Duplicate dialog's twin (same copy engine, same response
 * envelope), and the properties below are copied because they were DECISIONS:
 *
 * 1. **The due date starts BLANK and is sent as an explicit null.** A template
 *    exists to re-run a job that already ran, so inheriting a date would be
 *    maximally wrong here: the new job would be born overdue — red on the
 *    dispatch board, counted against OTD — for a promise nobody made.
 *
 * 2. **A nest-bearing template's quantity is DERIVED, not typed.** The field is
 *    DISABLED (not hidden, and carrying its reason) and nothing is sent; the
 *    server derives the quantity from the copied nests' planned runs. The
 *    success toast therefore quotes the RESPONSE — the fixture makes the stored
 *    quantity DIFFER from the prefill so a regression to the form value fails.
 *
 * 3. **Quantity is OMITTED when blank, never fabricated.** Omitted is what lets
 *    the server resolve the template's default and then the source work order's
 *    own quantity, and refuse 422 if neither is positive.
 *
 * 4. **A PARTIAL copy stops the flow; a clean one stays one click.** A skipped
 *    material tie means the new job carries NO demand for that material: no
 *    shortage is raised, the nests run, and stock is never deducted. A toast
 *    self-dismisses on a timer and fires while the caller is navigating away, so
 *    the partial path renders the shared skip report instead — no toast, no
 *    hand-off, no auto-close — and BOTH branches are asserted, because a
 *    regression in either direction is silent.
 *
 * 5. **A deleted source work order does NOT refuse the copy.** It cannot: the FK
 *    is NOT NULL with no `ON DELETE`, so the source can only ever be SOFT-deleted
 *    and keeps its whole plan. The form renders as usual and the deletion is a
 *    muted disclosure above it.
 *
 * 6. **A genuinely unusable template is refused in words, with no submit
 *    control.** `available = false` means the server cannot resolve the source and
 *    will refuse the use 409. The dialog reads `template.plan` off the prop rather
 *    than re-fetching, so this branch is reachable only from a stale list — which
 *    is exactly why it stays.
 *
 * Mocks `services/api`; the wire shapes are pinned in
 * `services/api.workOrderTemplates.test.ts`. Read both together.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import UseTemplateModal from './UseTemplateModal';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type {
  WorkOrder,
  WorkOrderDuplicateResult,
  WorkOrderDuplicateSkippedAllocation,
  WorkOrderDuplicateSkippedOperation,
  WorkOrderTemplate,
  WorkOrderTemplatePlan,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
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
  open_material_tie_count: 1,
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

const makeWorkOrder = (overrides: Partial<WorkOrder> = {}): WorkOrder => ({
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
  ...overrides,
});

const makeResult = (overrides: Partial<WorkOrderDuplicateResult> = {}): WorkOrderDuplicateResult => ({
  work_order: makeWorkOrder(),
  skipped_operations: [],
  skipped_material_allocations: [],
  ...overrides,
});

const skippedTie = (
  overrides: Partial<WorkOrderDuplicateSkippedAllocation> = {}
): WorkOrderDuplicateSkippedAllocation => ({
  source_allocation_id: 9,
  part_id: 55,
  source_work_order_operation_id: null,
  reason: 'part_not_available',
  ...overrides,
});

const skippedOperation = (
  overrides: Partial<WorkOrderDuplicateSkippedOperation> = {}
): WorkOrderDuplicateSkippedOperation => ({
  source_operation_id: 71,
  operation_number: 'Nest 3',
  sequence: 30,
  reason: 'laser_nest_deleted',
  ...overrides,
});

function renderModal({
  open = true,
  template = makeTemplate(),
}: { open?: boolean; template?: WorkOrderTemplate | null } = {}) {
  const onClose = jest.fn();
  const onUsed = jest.fn();
  const utils = render(
    <ToastProvider>
      <UseTemplateModal open={open} template={template} onClose={onClose} onUsed={onUsed} />
    </ToastProvider>
  );
  return { ...utils, onClose, onUsed };
}

const quantityInput = () => screen.getByLabelText(/Quantity/i) as HTMLInputElement;
const dueDateInput = () => screen.getByLabelText(/Due date/i) as HTMLInputElement;
const createButton = () => screen.getByRole('button', { name: /Creat/i });
const cancelButton = () => screen.getByRole('button', { name: 'Cancel' });
const skipsPanel = () => screen.getByTestId('duplicate-wo-skips');
const dismissButton = () => screen.getByRole('button', { name: 'Dismiss' });
const goToCopyButton = () => screen.getByRole('button', { name: /^Go to / });

/** Submit a use the server answers PARTIALLY, and wait for the result view. */
async function submitPartialCopy(result: WorkOrderDuplicateResult, template = makeTemplate()) {
  mockApi.useWorkOrderTemplate.mockResolvedValue(result);
  const rendered = renderModal({ template });
  await userEvent.click(createButton());
  await screen.findByTestId('duplicate-wo-skips');
  return rendered;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.useWorkOrderTemplate.mockResolvedValue(makeResult());
});

describe('UseTemplateModal: the two fields the planner controls', () => {
  it('prefills the quantity from the template default', async () => {
    renderModal();
    await waitFor(() => expect(quantityInput()).toHaveValue(12));
  });

  it('falls back to the source work order quantity when the template has no default', async () => {
    renderModal({ template: makeTemplate({ default_quantity: null }) });
    await waitFor(() => expect(quantityInput()).toHaveValue(50));
  });

  it('leaves the due date BLANK and sends it as an explicit null', async () => {
    // The whole point: a template re-runs a job that already ran, so a carried
    // date would make the new one overdue on sight, on the board and in OTD.
    renderModal();
    await waitFor(() => expect(dueDateInput()).toHaveValue(''));

    await userEvent.click(createButton());

    await waitFor(() =>
      expect(mockApi.useWorkOrderTemplate).toHaveBeenCalledWith(7, {
        quantity_ordered: 12,
        due_date: null,
      })
    );
  });

  it('sends the typed due date and quantity when the planner sets them', async () => {
    renderModal();
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.type(dueDateInput(), '2026-09-30');
    await userEvent.clear(quantityInput());
    await userEvent.type(quantityInput(), '25');
    await userEvent.click(createButton());

    await waitFor(() =>
      expect(mockApi.useWorkOrderTemplate).toHaveBeenCalledWith(7, {
        quantity_ordered: 25,
        due_date: '2026-09-30',
      })
    );
  });

  it('OMITS the quantity when the field is cleared rather than fabricating one', async () => {
    // Omitting is what lets the server resolve the template's default and then
    // the source work order's quantity — and refuse 422 if neither is positive.
    // A fabricated 1 on a job that should have run fifty is a plan nobody approved.
    renderModal();
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.clear(quantityInput());
    await userEvent.click(createButton());

    await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalled());
    const [, payload] = mockApi.useWorkOrderTemplate.mock.calls[0];
    expect(payload).toEqual({ due_date: null });
  });

  it('names the template in the dialog title and shows its note', async () => {
    renderModal({ template: makeTemplate({ notes: 'Runs on the Ermaksan.' }) });

    expect(await screen.findByText('Use template — Bracket brake set')).toBeInTheDocument();
    expect(screen.getByTestId('use-template-notes')).toHaveTextContent('Runs on the Ermaksan.');
  });
});

describe('UseTemplateModal: a nest-bearing quantity is derived, not typed', () => {
  const nestTemplate = makeTemplate({
    name: 'Miratech nest group',
    default_quantity: null,
    plan: makePlan({
      work_order_type: 'laser_cutting',
      sequential_operations: false,
      nest_count: 21,
      planned_runs_total: 63,
      operation_count: 21,
      source_quantity_ordered: 63,
      work_centers: ['LASER-1'],
    }),
  });

  it('locks the field and says why', async () => {
    renderModal({ template: nestTemplate });

    expect(quantityInput()).toBeDisabled();
    expect(screen.getByText(/Derived, not typed/i)).toBeInTheDocument();
    expect(screen.getByText(/sum of its nests’ sheet runs/i)).toBeInTheDocument();
  });

  it('sends no quantity at all — the server derives it from the copied nests', async () => {
    renderModal({ template: nestTemplate });

    await userEvent.click(createButton());

    await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalledWith(7, { due_date: null }));
  });

  it('quotes the STORED quantity in the toast, not the one on the form', async () => {
    // The server DERIVED 63; nothing on the form said so. Quoting the form here
    // shows a planner a number the server did not keep.
    mockApi.useWorkOrderTemplate.mockResolvedValue(
      makeResult({ work_order: makeWorkOrder({ quantity_ordered: 63, work_order_type: 'laser_cutting' }) })
    );
    renderModal({ template: nestTemplate });

    await userEvent.click(createButton());

    expect(
      await screen.findByText(/WO-20260825-002 created as a draft — qty 63, from template "Miratech nest group"/)
    ).toBeInTheDocument();
  });
});

describe('UseTemplateModal: a clean copy stays one click', () => {
  it('toasts, hands the WHOLE envelope over, and closes', async () => {
    const result = makeResult();
    mockApi.useWorkOrderTemplate.mockResolvedValue(result);
    const { onClose, onUsed } = renderModal();

    await userEvent.click(createButton());

    await waitFor(() => expect(onUsed).toHaveBeenCalledWith(result));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText(/WO-20260825-002 created as a draft — qty 12, from template "Bracket brake set"/)
    ).toBeInTheDocument();
    // The draft is the point of the feature: nothing reaches the floor until
    // somebody releases it, and the toast has to say so.
    expect(screen.getByText(/Review it, then release/)).toBeInTheDocument();
  });

  it('renders no skip report when both lists are empty', async () => {
    renderModal();
    await userEvent.click(createButton());

    await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalled());
    expect(screen.queryByTestId('duplicate-wo-skips')).not.toBeInTheDocument();
  });
});

describe('UseTemplateModal: a partial copy stops the flow', () => {
  const partial = (overrides: Partial<WorkOrderDuplicateResult> = {}) =>
    makeResult({ skipped_material_allocations: [skippedTie()], ...overrides });

  it('swaps the form for a RESULT view naming the new work order', async () => {
    await submitPartialCopy(partial());

    expect(screen.getByText('Created with omissions — WO-20260825-002')).toBeInTheDocument();
    expect(screen.queryByText('Use template — Bracket brake set')).not.toBeInTheDocument();
    // The form is gone — there is nothing left to fill in.
    expect(screen.queryByLabelText(/Quantity/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Due date/i)).not.toBeInTheDocument();

    const panel = skipsPanel();
    expect(panel).toHaveTextContent('WO-20260825-002 created as a draft — qty 12');
    expect(panel).toHaveTextContent('from template Bracket brake set');
    expect(panel).toHaveTextContent('Not copied: 1 material tie.');
    expect(panel).toHaveTextContent('Check the new work order before releasing it.');
    // A result, NOT a refusal — the work order exists and is a valid draft.
    expect(screen.queryByTestId('use-template-error')).not.toBeInTheDocument();
  });

  it('does NOT report the copy and does NOT close on submit', async () => {
    // The whole fork. Firing `onUsed` here navigates the planner away and
    // unmounts the only surface that names the omission.
    const { onClose, onUsed } = await submitPartialCopy(partial());

    expect(onUsed).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('does not ALSO toast — one surface, or people learn to read neither', async () => {
    await submitPartialCopy(partial());

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // The panel itself is role="status", so assert it is the ONLY one.
    const announced = screen.getAllByRole('status');
    expect(announced).toHaveLength(1);
    expect(announced[0]).toBe(skipsPanel());
    expect(announced[0]).not.toHaveTextContent('Review it, then release.');
  });

  it('stays on screen long past the moment a toast would have vanished', async () => {
    jest.useFakeTimers();
    try {
      const user = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
      mockApi.useWorkOrderTemplate.mockResolvedValue(partial());
      const { onClose, onUsed } = renderModal();
      await user.click(createButton());
      await screen.findByTestId('duplicate-wo-skips');

      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });

      expect(skipsPanel()).toBeInTheDocument();
      expect(onClose).not.toHaveBeenCalled();
      expect(onUsed).not.toHaveBeenCalled();
    } finally {
      jest.useRealTimers();
    }
  });

  it('names every skipped tie by part and reason, and spells out the consequence', async () => {
    await submitPartialCopy(
      partial({
        skipped_material_allocations: [
          skippedTie({ source_allocation_id: 9, part_id: 55, reason: 'part_not_available' }),
          skippedTie({ source_allocation_id: 10, part_id: 77, reason: 'part_not_tieable' }),
        ],
      })
    );

    const rows = within(screen.getByTestId('duplicate-wo-skipped-ties')).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Part #55');
    expect(rows[0]).toHaveTextContent('the tied part is no longer available');
    expect(rows[1]).toHaveTextContent('the tied part is one the shop produces, not stock material');

    const ties = screen.getByTestId('duplicate-wo-skipped-ties').parentElement!;
    expect(ties).toHaveTextContent(/Re-tie the material by hand before releasing it/i);
    expect(ties).toHaveTextContent(/no shortage shows, the nests run, and stock is never deducted/i);
  });

  it('names a skipped NEST operation as a nest, not "Op Nest 3"', async () => {
    await submitPartialCopy(
      partial({ skipped_operations: [skippedOperation()], skipped_material_allocations: [] })
    );

    const [row] = within(screen.getByTestId('duplicate-wo-skipped-operations')).getAllByRole('listitem');
    expect(row).toHaveTextContent('Nest 3');
    expect(row).not.toHaveTextContent('Op Nest 3');
    expect(row).toHaveTextContent('its laser nest was deleted');
  });

  it('hands the caller the WHOLE envelope only once the planner chooses to go there', async () => {
    const result = partial();
    const { onClose, onUsed } = await submitPartialCopy(result);
    const calls: string[] = [];
    onUsed.mockImplementation(() => calls.push('used'));
    onClose.mockImplementation(() => calls.push('closed'));

    await userEvent.click(goToCopyButton());

    expect(onUsed).toHaveBeenCalledTimes(1);
    const [passed] = onUsed.mock.calls[0] as [WorkOrderDuplicateResult];
    expect(passed.work_order.id).toBe(501);
    expect(passed.skipped_material_allocations).toHaveLength(1);
    expect(calls).toEqual(['used', 'closed']);
  });

  it('DISMISS closes without navigating', async () => {
    const { onClose, onUsed } = await submitPartialCopy(partial());

    await userEvent.click(dismissButton());

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onUsed).not.toHaveBeenCalled();
  });

  it('moves focus onto the go-to control when the result view appears', async () => {
    await submitPartialCopy(partial());
    await waitFor(() => expect(goToCopyButton()).toHaveFocus());
  });

  it('refuses a stray backdrop click, but still closes on a deliberate Escape', async () => {
    const { onClose } = await submitPartialCopy(partial());

    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onClose).not.toHaveBeenCalled();
    expect(skipsPanel()).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});

describe('UseTemplateModal: server-gated, therefore non-optimistic', () => {
  it('renders the refusal VERBATIM and keeps the dialog open', async () => {
    mockApi.useWorkOrderTemplate.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'Cannot create a work order for a retired part.' },
      },
    });
    const { onClose, onUsed } = renderModal();

    await userEvent.click(createButton());

    expect(await screen.findByTestId('use-template-error')).toHaveTextContent(
      'Cannot create a work order for a retired part.'
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(onUsed).not.toHaveBeenCalled();
  });

  it('renders a STRUCTURED 409 detail readably rather than as [object Object]', async () => {
    // The process-sheet refusal is an object carrying a `code`. Rendering it raw
    // would put "[object Object]" in front of the planner.
    mockApi.useWorkOrderTemplate.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            code: 'PROCESS_SHEET_UNAVAILABLE',
            msg: 'Operation 20 has no released process sheet revision.',
          },
        },
      },
    });
    renderModal();

    await userEvent.click(createButton());

    expect(await screen.findByTestId('use-template-error')).toHaveTextContent(
      'Operation 20 has no released process sheet revision.'
    );
  });

  it('disables Cancel and refuses Escape while the write is in flight', async () => {
    let resolve!: (result: WorkOrderDuplicateResult) => void;
    mockApi.useWorkOrderTemplate.mockReturnValue(
      new Promise<WorkOrderDuplicateResult>((r) => {
        resolve = r;
      })
    );
    const { onClose } = renderModal();

    await userEvent.click(createButton());

    await waitFor(() => expect(cancelButton()).toBeDisabled());
    // The work order may already exist server-side.
    await userEvent.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();

    resolve(makeResult());
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});

describe('UseTemplateModal: a deleted source work order still copies', () => {
  const deletedSourceTemplate = makeTemplate({
    plan: makePlan({ source_work_order_deleted: true }),
  });

  it('renders the form and the submit control, exactly as for a live source', async () => {
    renderModal({ template: deletedSourceTemplate });

    // A soft-deleted work order keeps every operation, nest and tie it had, so
    // there is nothing to refuse — and refusing was the bug.
    expect(screen.queryByTestId('use-template-unavailable')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Create draft work order/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/Quantity/i)).toBeEnabled();
  });

  it('discloses the deletion as a muted line, not an alert', async () => {
    renderModal({ template: deletedSourceTemplate });

    const note = screen.getByTestId('use-template-source-deleted');
    expect(note).toHaveTextContent(/That work order has been deleted/i);
    // `role="alert"` is the treatment the refusal box and the server-error box get.
    // This is context: it interrupts nobody.
    expect(note).not.toHaveAttribute('role', 'alert');
  });

  it('says nothing at all when the source is live', async () => {
    renderModal();
    expect(screen.queryByTestId('use-template-source-deleted')).not.toBeInTheDocument();
  });
});

describe('UseTemplateModal: an unusable template is refused in words', () => {
  const deadTemplate = makeTemplate({
    plan: makePlan({
      available: false,
      unavailable_reason: 'source_work_order_deleted',
      source_work_order_number: null,
      source_status: null,
      operation_count: 0,
      open_material_tie_count: 0,
      work_centers: [],
      source_quantity_ordered: null,
    }),
  });

  it('offers no submit control at all, and names the cause', async () => {
    // A pre-change server (or a list cached from one) is the only thing that sends
    // this shape now — the current server reads the plan through a deleted source.
    // A stale row must not submit a write the server it came from will refuse.
    renderModal({ template: deadTemplate });

    expect(screen.getByTestId('use-template-unavailable')).toHaveTextContent(
      /The work order this template was saved from has been deleted, and this copy was refused/i
    );
    expect(screen.queryByRole('button', { name: /Creat/i })).not.toBeInTheDocument();
    // No form to fill in either — nothing here can be submitted.
    expect(screen.queryByLabelText(/Quantity/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
  });

  it('shows an unknown reason token verbatim rather than guessing at a cause', async () => {
    // The server owns this vocabulary and says to treat the set as OPEN.
    renderModal({
      template: makeTemplate({ plan: makePlan({ available: false, unavailable_reason: 'source_company_closed' }) }),
    });

    expect(screen.getByTestId('use-template-unavailable')).toHaveTextContent('source_company_closed');
  });
});
