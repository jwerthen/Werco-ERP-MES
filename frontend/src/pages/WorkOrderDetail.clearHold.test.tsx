/**
 * WorkOrderDetail — Clear Hold, and the disclosure that has to come before it.
 *
 * The reported bug: a shop owner put a hold on a laser nest and could not clear
 * it. This page had NO control that lifted a hold — `resumeOperation` had zero
 * call sites here — so the only three doors in the whole app were shop-floor
 * screens (ShopFloorSimple's Check In, and the two kiosks). An office user
 * staring at a stuck job was offered exactly one button, Resolve, which 403s for
 * every role below supervisor.
 *
 * What these tests pin, in the order the user meets it:
 *
 * 1. **The reason renders BEFORE the click.** Reason and attribution are
 *    INDEPENDENT: a bare hold (mis-tap — no note, category OTHER) files no
 *    blocker at all, so it has a holder and no reason, and a hold placed before
 *    either record existed has neither. Both are REAL states that must read
 *    sanely rather than as an error or an empty panel.
 * 2. **The confirm says what clearing does not do.** Clearing a hold does not
 *    close the blocker — the server deliberately keeps the two decoupled, so a
 *    dialog implying otherwise would let a live quality stop read as cleared.
 * 3. **The toast is honest.** `open_blockers` non-empty, or `status: "pending"`
 *    (resume RESTORES, it does not release — a PENDING landing stays off the
 *    dispatch board and the kiosk, which surface READY only), each earn the
 *    `warning` variant. Both at once compose ONE toast; two toasts about one
 *    click read as two failures.
 * 4. **Resolve is gated to the roles the server allows** (ADMIN/MANAGER/
 *    SUPERVISOR), and where it is hidden the copy points at the control the
 *    reader can actually use.
 *
 * Harness mirrors WorkOrderDetail.resolveBlockerToast.test.tsx (side channels
 * mocked, ToastProvider mounted so toast text actually renders).
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { WorkOrderBlocker } from '../types/aiForward';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    resolveWorkOrderBlocker: jest.fn(),
    resumeOperation: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    getMaterialAllocations: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = {
  id: 1,
  role: 'admin',
  is_superuser: false,
};

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, isAuthenticated: true, isLoading: false }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const HOLD_NOTE = 'Spindle bearing failed - do not run';

// A fully-recorded hold: a blocker explains it AND the event names the holder.
const FULL_HOLD = {
  held_at: '2026-08-11T19:14:00Z',
  held_by_user_id: 9,
  held_by_name: 'Dana R.',
  blocker: {
    id: 7,
    category: 'machine_down',
    severity: 'high',
    status: 'open',
    title: 'Machine Down: OP20 Deburr',
    note: HOLD_NOTE,
    // Never withheld on THIS response — the work-order endpoints serve an
    // identified office session, not an unattended station. `has_note` is
    // deliberately absent, which is exactly why nothing may gate on it.
    free_text_withheld: false,
    reported_at: '2026-08-11T19:14:00Z',
    reported_by_user_id: 9,
    reported_by_name: 'Dana R.',
  },
};

const heldOperation = (hold: unknown) => ({
  id: 71,
  work_order_id: 42,
  sequence: 20,
  operation_number: '20',
  name: 'Deburr',
  status: 'on_hold',
  quantity_complete: 0,
  estimated_hours: 1,
  hold_context: hold,
});

const RUNNING_OP = {
  id: 72,
  work_order_id: 42,
  sequence: 30,
  operation_number: '30',
  name: 'Final Inspect',
  status: 'in_progress',
  quantity_complete: 0,
  estimated_hours: 1,
  hold_context: null,
};

// The SAME hold, as the Blockers panel renders it. Its title is deliberately not
// the string the row's hold_context carries: both panels are on screen at once, so
// a shared title would make every query below ambiguous rather than wrong.
const BLOCKER: WorkOrderBlocker = {
  id: 7,
  company_id: 1,
  work_order_id: 42,
  operation_id: 71,
  material_part_id: null,
  category: 'machine_down',
  severity: 'high',
  status: 'open',
  title: 'Machine Down: Deburr cell',
  note: 'Awaiting the replacement bearing',
  reported_at: '2026-08-11T19:14:00Z',
  created_at: '2026-08-11T19:14:00Z',
  updated_at: '2026-08-11T19:14:00Z',
};

const workOrderFixture = (operations: unknown[]) => ({
  id: 42,
  version: 1,
  work_order_number: 'WO-0042',
  part_id: 100,
  work_order_type: 'production',
  quantity_ordered: 10,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'in_progress',
  priority: 3,
  estimated_hours: 8,
  actual_hours: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  operations,
});

function renderDetail() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/42']}>
        <Routes>
          <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

/**
 * The row action, selected by `title`. The confirm dialog's own button carries
 * the same accessible name ("Clear hold"), so a name-based query would match
 * both and every assertion below would be measuring the wrong one.
 */
const clearHoldButtons = () => screen.queryAllByTitle('Lift the hold on this operation');

/** Opens the confirm and presses its Clear hold button, flushing the whole chain in act. */
async function clearHoldViaDialog() {
  fireEvent.click(clearHoldButtons()[0]);
  const dialog = await screen.findByRole('dialog');
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: /^clear hold$/i }));
  });
  return dialog;
}

function setOperations(operations: unknown[]) {
  mockedApi.getWorkOrder.mockResolvedValue(workOrderFixture(operations));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'admin', is_superuser: false };

  setOperations([heldOperation(FULL_HOLD), RUNNING_OP]);
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.resumeOperation.mockResolvedValue({
    message: 'Operation resumed',
    status: 'ready',
    open_blockers: [],
  });
});

describe('WorkOrderDetail — why an operation is held (fix D)', () => {
  it('shows the reason, the written note and who held it, before any click', async () => {
    renderDetail();

    await screen.findByText('Deburr');
    expect(screen.getByText('Machine down · High')).toBeInTheDocument();
    expect(screen.getByText('Machine Down: OP20 Deburr')).toBeInTheDocument();
    // Read straight off `note`. `has_note` is not sent on this response, so a
    // `has_note` gate would have printed "no reason given" over this text.
    expect(screen.getByText(HOLD_NOTE)).toBeInTheDocument();
    expect(screen.getByText(/^Held by Dana R\./)).toBeInTheDocument();
  });

  it('names the holder on a BARE hold that filed no blocker (the mis-tap case)', async () => {
    // Reason and attribution are independent: no blocker, but the operation_hold
    // event still says who and when. Gating one on the other would render the
    // accidental hold as both anonymous and reasonless.
    setOperations([
      heldOperation({ held_at: '2026-08-11T19:14:00Z', held_by_user_id: 9, held_by_name: 'Dana R.', blocker: null }),
    ]);
    renderDetail();

    await screen.findByText('Deburr');
    expect(screen.getByText('On hold — reason not recorded')).toBeInTheDocument();
    expect(screen.getByText(/^Held by Dana R\./)).toBeInTheDocument();
  });

  it('renders sanely when NOTHING was recorded — not as an error', async () => {
    // A SERVED but empty block. `hold_contexts_for_operations` gives every id it is
    // asked about a key, so an operation with neither a blocker nor a hold event
    // arrives as an all-null OBJECT — never as a missing one. That distinction is
    // the whole point of the next test, so this fixture must not use null.
    setOperations([heldOperation({ held_at: null, held_by_user_id: null, held_by_name: null, blocker: null })]);
    renderDetail();

    await screen.findByText('Deburr');
    expect(screen.getByText('On hold — reason not recorded')).toBeInTheDocument();
    expect(screen.getByText('Who placed the hold was not recorded')).toBeInTheDocument();
    // Still offered — an unexplained hold is the one most worth clearing.
    expect(clearHoldButtons()).toHaveLength(1);
  });

  it('says NOTHING about the reason when the block did not arrive — never "not recorded"', async () => {
    // `hold_context: null` on a held row cannot mean "nothing was recorded" (that is
    // the all-null object above). It means the block was not SERVED: the row only
    // reads on_hold after `hydrateOperationsFromShopFloor` overwrote status from a
    // payload carrying no hold block, or the API predates the field.
    //
    // Rendering that as "reason not recorded" asserts, on a quality record and
    // directly above the control that lifts an AS9100D hold, that nobody ever wrote
    // one — over a hold that may have an open NCR-driven blocker behind it.
    setOperations([heldOperation(null)]);
    renderDetail();

    await screen.findByText('Deburr');
    expect(screen.queryByText('On hold — reason not recorded')).not.toBeInTheDocument();
    expect(screen.queryByText('Who placed the hold was not recorded')).not.toBeInTheDocument();
    // Still clearable: not knowing why must not be what strands the job.
    expect(clearHoldButtons()).toHaveLength(1);

    // And the confirm says the reason could not be LOADED, not that none exists.
    fireEvent.click(clearHoldButtons()[0]);
    expect(await screen.findByText(/Why it is held: could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText(/Why it is held: not recorded/)).not.toBeInTheDocument();
  });

  it('repeats the reason in the confirm and states that the blocker stays open', async () => {
    renderDetail();

    await screen.findByText('Deburr');
    fireEvent.click(clearHoldButtons()[0]);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Machine down · High');
    expect(dialog).toHaveTextContent(HOLD_NOTE);
    expect(dialog).toHaveTextContent(/Held by Dana R\./);
    expect(dialog).toHaveTextContent(/Clearing the hold does NOT close the blocker/);
    expect(dialog).toHaveTextContent(/returns to Pending/);
  });
});

describe('WorkOrderDetail — Clear Hold (fix A)', () => {
  it('offers the action only on a held row', async () => {
    renderDetail();

    await screen.findByText('Final Inspect');
    // Two operations on the fixture; only the ON_HOLD one carries the action.
    expect(clearHoldButtons()).toHaveLength(1);
  });

  it('resumes, refetches, and reports plain success when nothing is left over', async () => {
    renderDetail();
    await screen.findByText('Deburr');
    expect(mockedApi.getWorkOrder).toHaveBeenCalledTimes(1);

    await clearHoldViaDialog();

    await waitFor(() => expect(mockedApi.resumeOperation).toHaveBeenCalledWith(71));
    // Non-optimistic: the page reflects the server, so it refetches.
    await waitFor(() => expect(mockedApi.getWorkOrder).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('WO-0042 · Op 20: hold cleared.')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('warns — not succeeds — when the resume left a blocker open', async () => {
    mockedApi.resumeOperation.mockResolvedValue({
      message: 'Operation resumed',
      status: 'ready',
      open_blockers: [
        { id: 7, title: 'Machine Down: OP20 Deburr', category: 'machine_down', severity: 'high', status: 'open' },
      ],
    });
    renderDetail();
    await screen.findByText('Deburr');

    await clearHoldViaDialog();

    const toast = await findClearHoldToast();
    expect(toast).toHaveAttribute('role', 'alert');
    expect(toast.textContent).toContain('Machine Down: OP20 Deburr');
    expect(toast.textContent).toContain('does not close a blocker');
    // The green claim must not also be on screen.
    expect(screen.queryByText('WO-0042 · Op 20: hold cleared.')).not.toBeInTheDocument();
  });

  it('warns when the hold lifted but the job did NOT go back on the board', async () => {
    mockedApi.resumeOperation.mockResolvedValue({
      message: 'Operation resumed',
      status: 'pending',
      open_blockers: [],
    });
    renderDetail();
    await screen.findByText('Deburr');

    await clearHoldViaDialog();

    const toast = await findClearHoldToast();
    expect(toast).toHaveAttribute('role', 'alert');
    expect(toast.textContent).toContain('did NOT go back on the board');
    expect(screen.queryByText('WO-0042 · Op 20: hold cleared.')).not.toBeInTheDocument();
  });

  it('composes ONE toast when both shortfalls happen at once', async () => {
    mockedApi.resumeOperation.mockResolvedValue({
      message: 'Operation resumed',
      status: 'pending',
      open_blockers: [
        { id: 7, title: 'Machine Down: OP20 Deburr', category: 'machine_down', severity: 'high', status: 'open' },
      ],
    });
    renderDetail();
    await screen.findByText('Deburr');

    await clearHoldViaDialog();

    const toast = await findClearHoldToast();
    expect(toast.textContent).toContain('did NOT go back on the board');
    expect(toast.textContent).toContain('Machine Down: OP20 Deburr');
    // Exactly one — two stacked toasts about one click read as two failures.
    const all = screen.getAllByRole('alert').filter((el) => el.textContent?.includes('hold cleared'));
    expect(all).toHaveLength(1);
  });

  it('shows the server refusal verbatim and leaves the row on hold', async () => {
    // The cancelled-nest tombstone reaches the user THIS way: this branch's
    // operation payload carries no `cancelled_nest_id`, so the page cannot gate
    // the button before the click — the non-optimistic path is what keeps that
    // honest, by rendering the server's own reason and moving nothing.
    const refusal = 'This nest was cancelled; its operation cannot be resumed.';
    mockedApi.resumeOperation.mockRejectedValue({ response: { data: { detail: refusal } } });
    renderDetail();
    await screen.findByText('Deburr');

    const dialog = await clearHoldViaDialog();

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    expect(screen.queryByText('WO-0042 · Op 20: hold cleared.')).not.toBeInTheDocument();
    // No refetch on a refusal, and the confirm stays up to be read against the row.
    expect(mockedApi.getWorkOrder).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /^clear hold$/i })).toBeEnabled()
    );
  });
});

describe('WorkOrderDetail — Resolve blocker role gate (fix F)', () => {
  beforeEach(() => {
    mockedApi.getWorkOrderBlockers.mockResolvedValue([BLOCKER]);
  });

  it.each([
    ['admin', false],
    ['manager', false],
    ['supervisor', false],
    ['platform_admin', false],
    ['viewer', true],
  ])('offers Resolve to %s (is_superuser=%s)', async (role, isSuperuser) => {
    mockUser = { id: 2, role, is_superuser: isSuperuser as boolean };
    renderDetail();

    expect(await screen.findByRole('button', { name: /^resolve$/i })).toBeInTheDocument();
  });

  it.each(['operator', 'quality', 'shipping', 'viewer'])(
    'hides Resolve from %s and says who closes a blocker instead',
    async (role) => {
      mockUser = { id: 3, role, is_superuser: false };
      renderDetail();

      // Wait for the blocker card itself, so this cannot pass on an unloaded page.
      await screen.findByText('Machine Down: Deburr cell');
      expect(screen.queryByRole('button', { name: /^resolve$/i })).not.toBeInTheDocument();
      expect(screen.getByText(/A supervisor or manager closes a blocker/)).toBeInTheDocument();
      // And the thing they CAN do is still on the page.
      expect(clearHoldButtons()).toHaveLength(1);
    }
  );
});

/**
 * The toast the Clear Hold click produced. Matched on the message rather than on
 * `role`, because other panels on this page render `role="alert"` too.
 */
async function findClearHoldToast(): Promise<HTMLElement> {
  return waitFor(() => {
    const match = screen
      .getAllByRole('alert')
      .find((el) => el.textContent?.includes('hold cleared'));
    if (!match) throw new Error('no clear-hold toast yet');
    return match;
  });
}
