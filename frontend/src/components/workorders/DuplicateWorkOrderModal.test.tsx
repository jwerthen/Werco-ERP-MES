/**
 * Duplicate a work order — the dialog that decides two things and inherits the rest.
 *
 * Four properties here are the reason the file exists, and each of them is a
 * decision the obvious implementation gets wrong:
 *
 * 1. **The due date starts BLANK.** Inheriting the source's date would make a
 *    re-run of last month's job born overdue — red on the dispatch board, counted
 *    against OTD — for a promise nobody made. A missing date reads as
 *    "unscheduled"; a stale one reads as "late".
 *
 * 2. **A nest-bearing job's quantity is DERIVED, not typed.** `quantity_ordered`
 *    on a laser work order is defined as the sum of its nests' planned runs, and
 *    the server overrules whatever this form sends. So the field is DISABLED, not
 *    hidden, and carries the reason: a missing field is a mystery, a disabled one
 *    with an explanation is not. The value still rides along because the schema
 *    requires a positive number.
 *
 * 3. **The success toast quotes the RESPONSE, never the form.** On a nest-bearing
 *    source the stored quantity is not the submitted one, so quoting the typed
 *    value would show the planner a number the server did not store. The test
 *    deliberately makes the two DIFFER, so a regression to `quantity` fails.
 *
 * 4. **A partial copy STOPS THE FLOW; a clean one stays one click.** The server
 *    returns an envelope whose skip lists say what it could not carry across. A
 *    skipped material tie means the new job has NO demand for that material: no
 *    shortage is raised, the nests run, and stock is never deducted.
 *
 *    A toast cannot carry that news. It self-dismisses after 4s and it fires while
 *    the caller is navigating to the new work order, so the one surface that named
 *    the omission is gone before the destination has painted, and nothing there
 *    re-states it. So the partial path renders a RESULT VIEW instead — no toast, no
 *    hand-off, no close — that itemizes what was lost and makes the planner choose
 *    "go to the copy" or "dismiss". Both branches are asserted, because a regression
 *    in either direction is silent, and the result view is asserted to PERSIST:
 *    "it went away on its own" is the exact failure the fork exists to prevent.
 *
 *    It is amber, not red. The work order EXISTS and is a valid draft; rendering it
 *    as a failure sends someone hunting for a job that is already there.
 *
 * 5. **Server-gated, therefore non-optimistic.** Nothing is painted before the
 *    server answers: while in flight Cancel is disabled and Escape/backdrop are
 *    refused (the copy may already exist), and a refusal keeps the dialog OPEN
 *    with the server's `detail` rendered verbatim rather than closing over it.
 *
 * Plus the list-page seam: with no `hasLaserNests` prop the dialog resolves
 * nest-ness itself with one `getWorkOrder` read. Both edges of that read are
 * covered — the unresolved window (locked, so a planner is never invited to type
 * a number about to become un-typeable) and the failed read (fall back to the
 * ordinary editable field rather than a permanently locked one).
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS FILE CANNOT CATCH
 * ---------------------------------------------------------------------------
 * It mocks `services/api`, so every assertion below is against the DECLARED
 * return type of `api.duplicateWorkOrder`, not against what the server sends. A
 * mismatch between the two is invisible here by construction — that is the SPC
 * failure mode (CLAUDE.md → type-check). The wire shape is pinned separately, at
 * the client boundary, in `services/api.duplicateWorkOrder.test.ts`; if the
 * server's envelope changes, that is the file that fails, and this one keeps
 * passing over a broken app. Read both together.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DuplicateWorkOrderModal, { DuplicateWorkOrderSource } from './DuplicateWorkOrderModal';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type {
  LaserNestInfo,
  WorkOrder,
  WorkOrderDuplicateResult,
  WorkOrderDuplicateSkippedAllocation,
  WorkOrderDuplicateSkippedOperation,
  WorkOrderOperation,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    duplicateWorkOrder: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const SOURCE: DuplicateWorkOrderSource = {
  id: 42,
  work_order_number: 'WO-20260501-004',
  quantity_ordered: 12,
};

const makeNest = (overrides: Partial<LaserNestInfo> = {}): LaserNestInfo => ({
  id: 9,
  nest_name: 'NEST-07',
  cnc_file_name: '05749.pdf',
  cnc_number: '05749',
  planned_runs: 6,
  completed_runs: 0,
  remaining_runs: 6,
  material: 'A36',
  ...overrides,
});

const makeOperation = (overrides: Partial<WorkOrderOperation> = {}): WorkOrderOperation => ({
  id: 71,
  version: 1,
  work_order_id: 42,
  work_center_id: 3,
  sequence: 10,
  operation_number: 'OP10',
  name: 'Laser',
  setup_time_hours: 0.5,
  run_time_hours: 2,
  run_time_per_piece: 0.1,
  actual_setup_hours: 0,
  actual_run_hours: 0,
  status: 'pending',
  quantity_complete: 0,
  quantity_scrapped: 0,
  requires_inspection: false,
  inspection_complete: false,
  created_at: '2026-05-01T12:00:00Z',
  updated_at: '2026-05-01T12:00:00Z',
  ...overrides,
});

const makeWorkOrder = (overrides: Partial<WorkOrder> = {}): WorkOrder => ({
  id: 99,
  version: 1,
  work_order_number: 'WO-20260805-001',
  part_id: 10,
  work_order_type: 'production',
  quantity_ordered: 12,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'draft',
  priority: 3,
  estimated_hours: 0,
  actual_hours: 0,
  created_at: '2026-08-05T12:00:00Z',
  updated_at: '2026-08-05T12:00:00Z',
  operations: [],
  ...overrides,
});

/** What the server actually returns: the new work order plus what it could not carry. */
const makeResult = (overrides: Partial<WorkOrderDuplicateResult> = {}): WorkOrderDuplicateResult => ({
  work_order: makeWorkOrder(),
  skipped_operations: [],
  skipped_material_allocations: [],
  ...overrides,
});

const skippedOperation = (
  overrides: Partial<WorkOrderDuplicateSkippedOperation> = {}
): WorkOrderDuplicateSkippedOperation => ({
  source_operation_id: 71,
  operation_number: 'OP20',
  sequence: 20,
  reason: 'laser_nest_deleted',
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

const axiosError = (status: number, detail: unknown) =>
  Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });

/** A promise the test resolves by hand, for asserting the in-flight window. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderModal({
  open = true,
  workOrder = SOURCE as DuplicateWorkOrderSource | null,
  hasLaserNests,
}: { open?: boolean; workOrder?: DuplicateWorkOrderSource | null; hasLaserNests?: boolean } = {}) {
  const onClose = jest.fn();
  const onDuplicated = jest.fn();
  const utils = render(
    <ToastProvider>
      <DuplicateWorkOrderModal
        open={open}
        workOrder={workOrder}
        hasLaserNests={hasLaserNests}
        onClose={onClose}
        onDuplicated={onDuplicated}
      />
    </ToastProvider>
  );
  return { ...utils, onClose, onDuplicated };
}

const quantityInput = () => screen.getByLabelText(/Quantity/i) as HTMLInputElement;
const dueDateInput = () => screen.getByLabelText(/Due date/i) as HTMLInputElement;
const duplicateButton = () => screen.getByRole('button', { name: /Duplicat/i });
const cancelButton = () => screen.getByRole('button', { name: 'Cancel' });

// The result view. `Dismiss` is matched exactly so it cannot collide with the
// toast list's own "Dismiss notification" control.
const skipsPanel = () => screen.getByTestId('duplicate-wo-skips');
const dismissButton = () => screen.getByRole('button', { name: 'Dismiss' });
const goToCopyButton = () => screen.getByRole('button', { name: /^Go to / });

/**
 * Submit a duplicate the server answers PARTIALLY, and wait for the result view.
 *
 * `hasLaserNests: false` throughout so the quantity field is live and the flow is
 * the ordinary one; nest-ness is covered on its own above.
 */
async function submitPartialCopy(result: WorkOrderDuplicateResult) {
  mockApi.duplicateWorkOrder.mockResolvedValue(result);
  const rendered = renderModal({ hasLaserNests: false });
  await waitFor(() => expect(quantityInput()).toBeEnabled());
  await userEvent.click(duplicateButton());
  await screen.findByTestId('duplicate-wo-skips');
  return rendered;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.duplicateWorkOrder.mockResolvedValue(makeResult());
  mockApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ id: 42, operations: [makeOperation()] }));
});

describe('DuplicateWorkOrderModal: the two fields the planner controls', () => {
  it('prefills the quantity from the source work order', async () => {
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toHaveValue(12));
  });

  it('leaves the due date BLANK rather than inheriting the source date', async () => {
    // The whole point: a copy born with last month's date is overdue on sight,
    // on the dispatch board and in OTD, for a promise nobody made.
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(dueDateInput()).toHaveValue(''));

    await userEvent.click(duplicateButton());
    await waitFor(() => expect(mockApi.duplicateWorkOrder).toHaveBeenCalled());
    expect(mockApi.duplicateWorkOrder).toHaveBeenCalledWith(42, {
      quantity_ordered: 12,
      // Blank means "no promise yet" — sent as an explicit null, not omitted.
      due_date: null,
    });
  });

  it('sends the typed due date when the planner sets one', async () => {
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.type(dueDateInput(), '2026-09-30');
    await userEvent.clear(quantityInput());
    await userEvent.type(quantityInput(), '25');
    await userEvent.click(duplicateButton());

    await waitFor(() =>
      expect(mockApi.duplicateWorkOrder).toHaveBeenCalledWith(42, {
        quantity_ordered: 25,
        due_date: '2026-09-30',
      })
    );
  });

  it('names the source work order in the dialog title', async () => {
    renderModal({ hasLaserNests: false });
    expect(await screen.findByText(/Duplicate work order — WO-20260501-004/)).toBeInTheDocument();
  });
});

describe('DuplicateWorkOrderModal: a nest-bearing quantity is derived, not typed', () => {
  it('locks the quantity field and explains why, while still sending the source quantity', async () => {
    // Disabled, never hidden — and the request still carries a positive number
    // because the schema requires one; the server simply overrules it.
    renderModal({ hasLaserNests: true });

    await waitFor(() => expect(quantityInput()).toBeDisabled());
    expect(screen.getByText(/Derived, not typed/i)).toBeInTheDocument();
    expect(screen.getByText(/sum of its nests’ sheet runs/i)).toBeInTheDocument();

    await userEvent.click(duplicateButton());
    await waitFor(() =>
      expect(mockApi.duplicateWorkOrder).toHaveBeenCalledWith(42, {
        quantity_ordered: 12,
        due_date: null,
      })
    );
  });

  it('does not mark the locked quantity required', async () => {
    // Nothing about it is the planner's to supply, so an asterisk would be a lie.
    renderModal({ hasLaserNests: true });
    await waitFor(() => expect(quantityInput()).toBeDisabled());
    expect(quantityInput()).not.toHaveAttribute('aria-required', 'true');
  });

  it('trusts the caller and reads nothing when hasLaserNests is supplied', async () => {
    // The detail page already has the operations loaded; a re-read would be a
    // second round trip for a fact the caller just handed over.
    renderModal({ hasLaserNests: true });
    await waitFor(() => expect(quantityInput()).toBeDisabled());
    expect(mockApi.getWorkOrder).not.toHaveBeenCalled();
  });

  it('fetches nothing at all while closed', async () => {
    renderModal({ open: false, hasLaserNests: undefined });
    await Promise.resolve();
    expect(mockApi.getWorkOrder).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('DuplicateWorkOrderModal: resolving nest-ness from the list page', () => {
  it('reads the source work order when the caller cannot say', async () => {
    mockApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ id: 42, operations: [makeOperation({ laser_nest: makeNest() })] })
    );
    renderModal();

    await waitFor(() => expect(mockApi.getWorkOrder).toHaveBeenCalledWith(42));
    await waitFor(() => expect(quantityInput()).toBeDisabled());
    expect(screen.getByText(/Derived, not typed/i)).toBeInTheDocument();
  });

  it('unlocks the field when the read comes back with no nests', async () => {
    mockApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ id: 42, operations: [makeOperation()] }));
    renderModal();

    await waitFor(() => expect(quantityInput()).toBeEnabled());
    expect(screen.getByText(/Prefilled from WO-20260501-004/i)).toBeInTheDocument();
  });

  it('keeps the field locked while the read is still in flight', async () => {
    // Never invite a number that is about to become un-typeable. The help text
    // says a check is running rather than silently showing a live-looking field.
    const pending = deferred<WorkOrder>();
    mockApi.getWorkOrder.mockReturnValue(pending.promise);
    renderModal();

    await waitFor(() => expect(quantityInput()).toBeDisabled());
    expect(screen.getByText(/Checking whether this job’s quantity comes from its nests/i)).toBeInTheDocument();

    pending.resolve(makeWorkOrder({ id: 42, operations: [makeOperation()] }));
    await waitFor(() => expect(quantityInput()).toBeEnabled());
  });

  it('falls back to an editable field when the read FAILS', async () => {
    // Could not tell. An ordinary editable field beats leaving a planner staring
    // at a locked one: for a nest-bearing source the server overrules the number
    // anyway, and the toast quotes what was actually stored.
    mockApi.getWorkOrder.mockRejectedValue(axiosError(500, 'boom'));
    renderModal();

    await waitFor(() => expect(quantityInput()).toBeEnabled());
    expect(screen.queryByText(/Derived, not typed/i)).not.toBeInTheDocument();
    // The failed probe must not surface as a submit error — nothing was submitted.
    expect(screen.queryByTestId('duplicate-wo-error')).not.toBeInTheDocument();
  });
});

describe('DuplicateWorkOrderModal: the >0 guard on an editable quantity', () => {
  it.each([
    ['blank', ''],
    ['zero', '0'],
    ['negative', '-4'],
  ])('refuses a %s quantity without calling the server', async (_label, value) => {
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.clear(quantityInput());
    if (value) await userEvent.type(quantityInput(), value);
    await userEvent.click(duplicateButton());

    expect(await screen.findByTestId('duplicate-wo-error')).toHaveTextContent(
      'Quantity must be greater than zero.'
    );
    expect(mockApi.duplicateWorkOrder).not.toHaveBeenCalled();
    // The button stays live: a dead control says nothing about why.
    expect(duplicateButton()).toBeEnabled();
  });

  it('marks an editable quantity required', async () => {
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());
    expect(quantityInput()).toHaveAttribute('aria-required', 'true');
  });
});

describe('DuplicateWorkOrderModal: server-gated, therefore non-optimistic', () => {
  it('holds the spinner, disables Cancel, and refuses Escape while the copy is in flight', async () => {
    const pending = deferred<WorkOrderDuplicateResult>();
    mockApi.duplicateWorkOrder.mockReturnValue(pending.promise);
    const { onClose, onDuplicated } = renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());

    // Spinner + double-click guard on the confirm button.
    const submitting = await screen.findByRole('button', { name: /Duplicating…/ });
    expect(submitting).toBeDisabled();
    // Cancel is refused: the copy may already exist server-side.
    await waitFor(() => expect(cancelButton()).toBeDisabled());
    // ... and so is Escape.
    await userEvent.keyboard('{Escape}');
    expect(onClose).not.toHaveBeenCalled();
    // Nothing is reported until the server answers.
    expect(onDuplicated).not.toHaveBeenCalled();

    pending.resolve(makeResult());
    await waitFor(() => expect(onDuplicated).toHaveBeenCalled());
  });

  it('does not fire twice when the confirm button is clicked twice', async () => {
    const pending = deferred<WorkOrderDuplicateResult>();
    mockApi.duplicateWorkOrder.mockReturnValue(pending.promise);
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());
    await userEvent.click(duplicateButton());
    expect(mockApi.duplicateWorkOrder).toHaveBeenCalledTimes(1);

    pending.resolve(makeResult());
    await waitFor(() => expect(mockApi.duplicateWorkOrder).toHaveBeenCalledTimes(1));
  });

  it('closes on Escape when NOT submitting', async () => {
    const { onClose } = renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('renders a refusal verbatim and keeps the dialog OPEN', async () => {
    mockApi.duplicateWorkOrder.mockRejectedValue(
      axiosError(409, 'Could not duplicate this work order; a generated record conflicts with an existing one.')
    );
    const { onClose, onDuplicated } = renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());

    expect(await screen.findByTestId('duplicate-wo-error')).toHaveTextContent(
      'Could not duplicate this work order; a generated record conflicts with an existing one.'
    );
    // Closing over a refusal would leave the planner believing the copy exists.
    expect(onClose).not.toHaveBeenCalled();
    expect(onDuplicated).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // Recoverable: the form is live again.
    expect(duplicateButton()).toBeEnabled();
    expect(cancelButton()).toBeEnabled();
  });

  it('never renders an object detail as [object Object]', async () => {
    mockApi.duplicateWorkOrder.mockRejectedValue(axiosError(409, { msg: 'This job is locked by a nest import' }));
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());
    const error = await screen.findByTestId('duplicate-wo-error');
    expect(error).toHaveTextContent('This job is locked by a nest import');
    expect(error).not.toHaveTextContent('[object Object]');
  });

  it('falls back to a readable message when the failure carries no detail', async () => {
    mockApi.duplicateWorkOrder.mockRejectedValue(new Error('Network Error'));
    renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());
    expect(await screen.findByTestId('duplicate-wo-error')).toHaveTextContent('Network Error');
  });
});

describe('DuplicateWorkOrderModal: a CLEAN copy reports success', () => {
  it('quotes the response quantity, not the typed one', async () => {
    // The load-bearing case: the request asked for 12, the server derived 18 from
    // the copied nests' runs. Quoting the form would show a number that is not in
    // the database.
    mockApi.duplicateWorkOrder.mockResolvedValue(
      makeResult({
        work_order: makeWorkOrder({ id: 501, work_order_number: 'WO-20260805-007', quantity_ordered: 18 }),
      })
    );
    const { onClose } = renderModal({ hasLaserNests: true });
    await waitFor(() => expect(quantityInput()).toBeDisabled());

    await userEvent.click(duplicateButton());

    // role="status" is the success variant — announced politely, nothing to act on.
    const toast = await screen.findByRole('status');
    expect(toast).toHaveTextContent('WO-20260805-007 created as a draft — qty 18');
    expect(toast).toHaveTextContent('copied from WO-20260501-004');
    expect(toast).toHaveTextContent('Review it, then release.');
    expect(toast).not.toHaveTextContent('qty 12');
    expect(toast).not.toHaveTextContent('Not copied');

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('hands the caller the WHOLE envelope, not just the work order', async () => {
    // Both callers navigate via `result.work_order.id`. Passing the bare work
    // order would compile at the call site and drop the skip lists on the floor —
    // and on a partial copy those lists are the only channel naming what was lost.
    // Asserted on BOTH paths, because they hand the envelope over at different
    // moments: here on submit, and below only once the planner chooses to go.
    const result = makeResult({
      work_order: makeWorkOrder({ id: 501, work_order_number: 'WO-20260805-007' }),
    });
    mockApi.duplicateWorkOrder.mockResolvedValue(result);
    const { onDuplicated } = renderModal({ hasLaserNests: false });
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());

    await waitFor(() => expect(onDuplicated).toHaveBeenCalledTimes(1));
    const [passed] = onDuplicated.mock.calls[0] as [WorkOrderDuplicateResult];
    expect(passed.work_order.id).toBe(501);
    // PRESENT and empty — that emptiness is the "nothing was lost" signal the
    // caller reads, so a bare work order carrying neither key is a different thing.
    expect(passed.skipped_operations).toEqual([]);
    expect(passed.skipped_material_allocations).toEqual([]);
  });

  it('reports the copy before navigating, in that order', async () => {
    const calls: string[] = [];
    const { onClose, onDuplicated } = renderModal({ hasLaserNests: false });
    onDuplicated.mockImplementation(() => calls.push('duplicated'));
    onClose.mockImplementation(() => calls.push('closed'));
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.click(duplicateButton());
    await waitFor(() => expect(calls).toEqual(['duplicated', 'closed']));
  });
});

describe('DuplicateWorkOrderModal: a PARTIAL copy stops the flow', () => {
  /**
   * The scenario this exists for: the source's sheet part was soft-deleted since
   * the job ran, so the material tie is skipped. Told "created as a draft", the
   * planner releases the laser work order believing it carries its material
   * demand — no shortage shows, the nests run, and stock is never deducted. The
   * omission is on the audit chain either way; this view is what puts it in front
   * of the person who can still act on it, and KEEPS it there.
   */
  const partial = (overrides: Partial<WorkOrderDuplicateResult> = {}) =>
    makeResult({
      work_order: makeWorkOrder({ id: 501, work_order_number: 'WO-20260805-007', quantity_ordered: 18 }),
      skipped_material_allocations: [skippedTie()],
      ...overrides,
    });

  it('swaps the form for a RESULT view that names the new work order', async () => {
    await submitPartialCopy(partial());

    // Not "Duplicate work order" any more: the copy exists, and a heading that
    // still reads like an unsubmitted form invites a second attempt.
    expect(screen.getByText('Copied with omissions — WO-20260805-007')).toBeInTheDocument();
    expect(screen.queryByText(/Duplicate work order — WO-20260501-004/)).not.toBeInTheDocument();
    // The form is gone — there is nothing left to fill in.
    expect(screen.queryByLabelText(/Quantity/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Due date/i)).not.toBeInTheDocument();

    const panel = skipsPanel();
    expect(panel).toHaveTextContent('WO-20260805-007 created as a draft — qty 18');
    expect(panel).toHaveTextContent('copied from WO-20260501-004');
    expect(panel).toHaveTextContent('Not copied: 1 material tie.');
    expect(panel).toHaveTextContent('Check the new work order before releasing it.');
    // A result, NOT a refusal: rendering this as an error sends someone hunting
    // for a job that is already there.
    expect(screen.queryByTestId('duplicate-wo-error')).not.toBeInTheDocument();
  });

  it('does not ALSO toast — one surface, or people learn to read neither', async () => {
    await submitPartialCopy(partial());

    // No warning/error toast...
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // ...and no success toast either. The panel itself is role="status", so the
    // assertion is that it is the ONLY one — a bare queryByRole('status') would
    // now match the panel and pass no matter how many toasts fired.
    const announced = screen.getAllByRole('status');
    expect(announced).toHaveLength(1);
    expect(announced[0]).toBe(skipsPanel());
    // And never the clean-copy sign-off: this copy is NOT ready to release as-is.
    expect(announced[0]).not.toHaveTextContent('Review it, then release.');
  });

  it('does NOT report the copy and does NOT close on submit', async () => {
    // The whole fork. Firing `onDuplicated` here navigates the planner away and
    // unmounts the only surface that names the omission.
    const { onClose, onDuplicated } = await submitPartialCopy(partial());

    expect(onDuplicated).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('stays on screen long past the moment a toast would have vanished', async () => {
    // THE property the fork buys. A toast self-dismisses after 4s — and it fires
    // while the caller is navigating — so the record of the omission disappears on
    // a timer nobody agreed to. This panel goes away when someone decides, not
    // when a timeout fires.
    jest.useFakeTimers();
    try {
      const user = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
      mockApi.duplicateWorkOrder.mockResolvedValue(partial());
      const { onClose, onDuplicated } = renderModal({ hasLaserNests: false });
      await waitFor(() => expect(quantityInput()).toBeEnabled());
      await user.click(duplicateButton());
      await screen.findByTestId('duplicate-wo-skips');

      await act(async () => {
        jest.advanceTimersByTime(30_000);
      });

      expect(skipsPanel()).toBeInTheDocument();
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(onClose).not.toHaveBeenCalled();
      expect(onDuplicated).not.toHaveBeenCalled();
    } finally {
      jest.useRealTimers();
    }
  });

  it('names every skipped TIE by part and reason — the point is naming, not counting', async () => {
    // A count tells the planner something is missing. Only the part number tells
    // them WHICH material to re-tie, which is the action the view exists to prompt.
    await submitPartialCopy(
      partial({
        skipped_material_allocations: [
          skippedTie({ source_allocation_id: 9, part_id: 55, reason: 'part_not_available' }),
          skippedTie({ source_allocation_id: 10, part_id: 77, reason: 'operation_not_copied' }),
        ],
      })
    );

    const rows = within(screen.getByTestId('duplicate-wo-skipped-ties')).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Part #55');
    expect(rows[0]).toHaveTextContent('the tied part is no longer available');
    expect(rows[1]).toHaveTextContent('Part #77');
    expect(rows[1]).toHaveTextContent('its operation was not copied');
  });

  it('names every skipped OPERATION by number and reason', async () => {
    await submitPartialCopy(
      partial({
        skipped_operations: [
          skippedOperation({ source_operation_id: 71, operation_number: 'OP20', sequence: 20 }),
          skippedOperation({ source_operation_id: 72, operation_number: 'OP30', sequence: 30 }),
        ],
        skipped_material_allocations: [],
      })
    );

    const rows = within(screen.getByTestId('duplicate-wo-skipped-operations')).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('OP20');
    expect(rows[0]).toHaveTextContent('its laser nest was deleted');
    expect(rows[1]).toHaveTextContent('OP30');
  });

  it('falls back through the ids when an operation carries no number', async () => {
    // The envelope's optional fields really are optional; a row that renders
    // nothing identifiable is a row the planner cannot act on.
    await submitPartialCopy(
      partial({
        skipped_operations: [
          skippedOperation({ source_operation_id: 71, operation_number: null, sequence: 20 }),
          skippedOperation({ source_operation_id: 72, operation_number: null, sequence: null }),
        ],
        skipped_material_allocations: [],
      })
    );

    const rows = within(screen.getByTestId('duplicate-wo-skipped-operations')).getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('Seq 20');
    expect(rows[1]).toHaveTextContent('Operation #72');
  });

  it('renders only the section it has something to say about', async () => {
    await submitPartialCopy(partial({ skipped_operations: [] }));

    expect(screen.getByTestId('duplicate-wo-skipped-ties')).toBeInTheDocument();
    expect(screen.queryByTestId('duplicate-wo-skipped-operations')).not.toBeInTheDocument();
  });

  it('spells out that the copy carries NO demand for the untied material', async () => {
    // The consequence, not just the fact: a job with no tie raises no shortage,
    // the nests run, and stock is never deducted.
    await submitPartialCopy(partial());

    const ties = screen.getByTestId('duplicate-wo-skipped-ties').parentElement!;
    expect(ties).toHaveTextContent(/Re-tie the material by hand before releasing it/i);
    expect(ties).toHaveTextContent(/no shortage shows, the nests run, and stock is never deducted/i);
  });

  it('hands the caller the WHOLE envelope only once the planner chooses to go there', async () => {
    const result = partial();
    const { onClose, onDuplicated } = await submitPartialCopy(result);
    const calls: string[] = [];
    onDuplicated.mockImplementation(() => calls.push('duplicated'));
    onClose.mockImplementation(() => calls.push('closed'));

    await userEvent.click(goToCopyButton());

    expect(onDuplicated).toHaveBeenCalledTimes(1);
    const [passed] = onDuplicated.mock.calls[0] as [WorkOrderDuplicateResult];
    expect(passed.work_order.id).toBe(501);
    // The lists ride along — a bare work order would compile and drop them.
    expect(passed.skipped_material_allocations).toHaveLength(1);
    // Reported before the close, same order as the clean path.
    expect(calls).toEqual(['duplicated', 'closed']);
  });

  it('labels the primary action with the work order it will take you to', async () => {
    await submitPartialCopy(partial());
    expect(goToCopyButton()).toHaveTextContent('Go to WO-20260805-007');
  });

  it('DISMISS closes without navigating', async () => {
    // Nothing should move the planner out from under a list of omissions they
    // just declined to follow. The copy still exists either way.
    const { onClose, onDuplicated } = await submitPartialCopy(partial());

    await userEvent.click(dismissButton());

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onDuplicated).not.toHaveBeenCalled();
  });

  it('moves focus onto the go-to control when the result view appears', async () => {
    // The button that had focus (Duplicate) just unmounted. A keyboard user must
    // not be left on <body> with a dialog full of unread omissions in front of them.
    await submitPartialCopy(partial());
    await waitFor(() => expect(goToCopyButton()).toHaveFocus());
  });

  it('refuses a stray backdrop click, but still closes on a deliberate Escape', async () => {
    const { onClose } = await submitPartialCopy(partial());

    // A misplaced click must not be what makes the only record of an un-copied
    // material tie disappear.
    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(onClose).not.toHaveBeenCalled();
    expect(skipsPanel()).toBeInTheDocument();

    // Escape is a deliberate keypress, and it is the keyboard's way out.
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});

describe('DuplicateWorkOrderModal: the summary states a reason only when it is sure of one', () => {
  /**
   * `skipSummary` used to assert "(its laser nest was deleted)" for ANY skipped
   * operation — true only while that is the only reason the server has. A confident
   * wrong explanation is worse than a bare count: it sends the planner to check the
   * nests when the real cause was something else entirely. So the parenthetical
   * appears only when every entry shares ONE reason the client has a phrase for.
   */
  const withOperations = (
    operations: WorkOrderDuplicateSkippedOperation[],
    ties: WorkOrderDuplicateSkippedAllocation[] = []
  ) => makeResult({ skipped_operations: operations, skipped_material_allocations: ties });

  it('names the shared reason when every skipped operation gives the same known one', async () => {
    await submitPartialCopy(withOperations([skippedOperation(), skippedOperation({ source_operation_id: 72 })]));
    expect(skipsPanel()).toHaveTextContent('Not copied: 2 operations (its laser nest was deleted).');
  });

  it('pluralizes each kind and counts both', async () => {
    await submitPartialCopy(
      makeResult({
        skipped_operations: [skippedOperation()],
        skipped_material_allocations: [
          skippedTie(),
          skippedTie({ source_allocation_id: 10, reason: 'nest_runs_unavailable' }),
          skippedTie({ source_allocation_id: 11, reason: 'operation_not_copied' }),
        ],
      })
    );
    // Ties are counted and never explained here — three different tie reasons
    // cannot honestly collapse into one parenthetical, and each row carries its
    // own reason in the itemized list below.
    expect(skipsPanel()).toHaveTextContent(
      'Not copied: 1 operation (its laser nest was deleted) and 3 material ties.'
    );
  });

  it('states NO reason when the skipped operations disagree', async () => {
    await submitPartialCopy(
      withOperations([
        skippedOperation({ source_operation_id: 71, reason: 'laser_nest_deleted' }),
        skippedOperation({ source_operation_id: 72, reason: 'routing_superseded' }),
      ])
    );

    const panel = skipsPanel();
    expect(panel).toHaveTextContent('Not copied: 2 operations. Check the new work order before releasing it.');
    // The subset explanation must not be applied to the whole set.
    expect(panel).not.toHaveTextContent('its laser nest was deleted');
  });

  it('states no reason for a token it has no phrase for, and shows that token verbatim', async () => {
    // The server owns this vocabulary and can grow it. An unknown reason must reach
    // the planner as itself, not be dropped and not be guessed at.
    await submitPartialCopy(
      withOperations([skippedOperation({ source_operation_id: 71, reason: 'routing_superseded' })])
    );

    expect(skipsPanel()).toHaveTextContent('Not copied: 1 operation. Check the new work order before releasing it.');
    const [row] = within(screen.getByTestId('duplicate-wo-skipped-operations')).getAllByRole('listitem');
    expect(row).toHaveTextContent('routing_superseded');
  });

  it('shows an unknown TIE reason verbatim too', async () => {
    await submitPartialCopy(
      makeResult({
        skipped_material_allocations: [skippedTie({ part_id: 55, reason: 'part_on_hold' })],
      })
    );

    expect(skipsPanel()).toHaveTextContent('Not copied: 1 material tie.');
    const [row] = within(screen.getByTestId('duplicate-wo-skipped-ties')).getAllByRole('listitem');
    expect(row).toHaveTextContent('Part #55');
    expect(row).toHaveTextContent('part_on_hold');
  });
});

describe('DuplicateWorkOrderModal: what the planner is told will happen', () => {
  it('says what carries and what stays with the source', async () => {
    // The copy is only trustworthy if the planner knows which half they are
    // getting; the panel names both halves and the draft landing state.
    renderModal({ hasLaserNests: false });
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByText(/Copies the plan/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/laser nests/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/open material ties/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/quantities, actual/i)).toBeInTheDocument();
    expect(within(dialog).getByText('draft')).toBeInTheDocument();
  });
});
