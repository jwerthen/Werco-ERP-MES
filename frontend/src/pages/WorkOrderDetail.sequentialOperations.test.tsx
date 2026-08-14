/**
 * WorkOrderDetail — the "Sequential operations" switch in the Operations /
 * Routing panel header.
 *
 * The motivating job is WO-20260807-006, a 4-operation weld assembly (10 Skid
 * Fit → 20 Wall Fit Up → 30 Accessory Fit Up → 40 Weld Out) whose first three
 * operations sit on ONE weld cell. READY promotion pools by work center, so all
 * three unlocked at once and the floor lost the build order. `sequential_
 * operations` on the work order is the per-WO discriminator between that pool
 * and a real sequenced routing, and this switch is how it gets flipped.
 *
 * Three properties are what this file exists to pin down, in order of how badly
 * they would hurt if they regressed:
 *
 * 1. NON-OPTIMISTIC. The switch renders `workOrder.sequential_operations` and
 *    nothing else. A refused write — 409 on a stale version, 403 on role — must
 *    leave it showing the value the server still holds, and must surface the
 *    server's `detail` verbatim. This is the CLAUDE.md convention for a
 *    server-GATED action: never show a state the server would refuse.
 *
 * 2. THE RE-READ IS LOAD-BEARING, not tidiness. Turning sequencing ON demotes
 *    un-started READY operations back to PENDING server-side, so the Status
 *    column sitting inches from this control is stale the instant the write
 *    lands. A local patch of just the flag would leave three weld operations
 *    reading "ready" under a rule that just unlocked one of them — which is
 *    exactly the confusion the feature exists to end. Hence the assertion below
 *    that the table's statuses come from the refetch.
 *
 * 3. NOT RENDERED ON A LASER WORK ORDER. The backend's
 *    `is_laser_dispatch_work_order` short-circuits ABOVE this flag at every seam
 *    and is strictly fuller (it drops predecessor gating entirely, across work
 *    centers), so the column is ignored there. A switch that writes a value the
 *    server never reads is worse than no switch: it would read as a broken
 *    feature the first time a nest still went READY out of order.
 *
 * Also covered: the absent-flag fallback (`?? false` — pooled is what a row
 * predating migration 081 actually behaves as), the exact `updateWorkOrder`
 * payload including `version` for the optimistic lock, the work_orders:edit role
 * gate, and the in-flight pending state.
 *
 * THE SECOND HALF of this file is the consequence of the switch: the office
 * per-operation "Complete" button must agree with the server's out-of-sequence
 * guard. `POST /work-orders/operations/{id}/complete` runs the shared
 * `operation_blocked_by_predecessors` and refuses 400 "Previous operations must
 * be completed first"; on a SEQUENCED routing that refusal covers every operation
 * above the lowest incomplete one, which on this very work order is two of the
 * three weld-cell rows. Offering Complete on all three sends the user through the
 * quantity/scrap modal to reach a 400 — and pooled, the same call SUCCEEDED, so
 * the switch is what turns a rare mismatch into the common one.
 *
 * The mirror has to be exact in BOTH directions, so the tests below pin the
 * refusals and the non-refusals equally: no work-center waiver under sequencing
 * (a predecessor blocks from its own cell too), nothing gated on a pooled work
 * order (today's behavior, byte for byte), and nothing gated on a laser WO even
 * when its ignored `sequential_operations` column happens to be true.
 *
 * Fixtures are typed as `WorkOrder` / `WorkOrderOperation` on purpose:
 * tsconfig.test.json type-checks this file, so a fixture that drifts from the
 * real contract is a compile error rather than a green test over a lie.
 *
 * Harness mirrors WorkOrderDetail.notesEdit.test.tsx (side-channels mocked, real
 * ToastProvider so toast text is assertable).
 */

import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { WorkOrder, WorkOrderOperation } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    // MaterialTiesPanel loads on mount; an unmocked method is `undefined` and
    // surfaces as a silent <ErrorState role="alert"> instead of a red test.
    getMaterialAllocations: jest.fn(),
    getWorkCenters: jest.fn(),
    getAIRecommendations: jest.fn(),
    // CompleteWorkModal -> useScrapReasonCodes fetches on open. The hook is
    // fail-soft, but mocking it keeps the modal tests off an accidental throw.
    getScrapReasonCodes: jest.fn(),
    updateWorkOrder: jest.fn(),
    updateOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

// Mutable so individual tests can exercise the role gate (jest allows lazily
// referenced `mock*` variables inside the hoisted factory).
let mockUser: { id: number; role: string; is_superuser: boolean } = {
  id: 1,
  role: 'admin',
  is_superuser: false,
};

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({
  useWebSocket: jest.fn(),
}));

jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

const SEQUENCED_HELP = 'Each operation unlocks when the previous one is complete';
const POOLED_HELP = 'Operations at the same work center can run in any order';

function makeOperation(overrides: Partial<WorkOrderOperation> = {}): WorkOrderOperation {
  return {
    id: 700,
    version: 1,
    work_order_id: 42,
    // The three fit-up operations deliberately SHARE work center 9 (the weld
    // cell) — that shared machine is the whole reason they pooled.
    work_center_id: 9,
    sequence: 10,
    name: 'Skid Fit',
    setup_time_hours: 0.5,
    run_time_hours: 2,
    run_time_per_piece: 0,
    actual_setup_hours: 0,
    actual_run_hours: 0,
    status: 'ready',
    quantity_complete: 0,
    quantity_scrapped: 0,
    requires_inspection: false,
    inspection_complete: false,
    created_at: '2026-08-07T00:00:00Z',
    updated_at: '2026-08-07T00:00:00Z',
    ...overrides,
  };
}

/** The three weld-cell operations as the POOL promoted them: all READY at once. */
const POOLED_OPERATIONS: WorkOrderOperation[] = [
  makeOperation({ id: 701, sequence: 10, name: 'Skid Fit', status: 'ready' }),
  makeOperation({ id: 702, sequence: 20, name: 'Wall Fit Up', status: 'ready' }),
  makeOperation({ id: 703, sequence: 30, name: 'Accessory Fit Up', status: 'ready' }),
  // Different work center, so it was PENDING even under the pooled rule.
  makeOperation({ id: 704, sequence: 40, name: 'Weld Out', work_center_id: 12, status: 'pending' }),
];

/** The same routing after the server re-ran promotion under the sequenced rule. */
const SEQUENCED_OPERATIONS: WorkOrderOperation[] = [
  makeOperation({ id: 701, sequence: 10, name: 'Skid Fit', status: 'ready' }),
  makeOperation({ id: 702, sequence: 20, name: 'Wall Fit Up', status: 'pending' }),
  makeOperation({ id: 703, sequence: 30, name: 'Accessory Fit Up', status: 'pending' }),
  makeOperation({ id: 704, sequence: 40, name: 'Weld Out', work_center_id: 12, status: 'pending' }),
];

/**
 * The same routing one step further on: 10 is COMPLETE, so the server's gate now
 * refuses 30 and 40 but no longer refuses 20.
 */
const SEQUENCED_OPERATIONS_10_COMPLETE: WorkOrderOperation[] = [
  makeOperation({
    id: 701,
    sequence: 10,
    name: 'Skid Fit',
    status: 'complete',
    quantity_complete: 2,
  }),
  makeOperation({ id: 702, sequence: 20, name: 'Wall Fit Up', status: 'ready' }),
  makeOperation({ id: 703, sequence: 30, name: 'Accessory Fit Up', status: 'pending' }),
  makeOperation({ id: 704, sequence: 40, name: 'Weld Out', work_center_id: 12, status: 'pending' }),
];

function makeWorkOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 42,
    version: 3,
    work_order_number: 'WO-20260807-006',
    part_id: 100,
    work_order_type: 'production',
    sequential_operations: false,
    quantity_ordered: 2,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 20,
    actual_hours: 0,
    created_at: '2026-08-07T00:00:00Z',
    updated_at: '2026-08-07T00:00:00Z',
    operations: POOLED_OPERATIONS,
    ...overrides,
  };
}

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

/** The switch, once the work order has loaded. */
const findSwitch = () => screen.findByRole('switch', { name: 'Sequential operations' });

/** Renders the page and waits for the switch — the shape most tests here want. */
async function renderAndFindSwitch(): Promise<HTMLElement> {
  renderDetail();
  return findSwitch();
}

/** The Operations / Routing table, scoped so status text can't match elsewhere. */
function routingTable(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Operations / Routing' });
  const card = heading.closest('div.card');
  if (!card) throw new Error('Operations / Routing card not found');
  return card as HTMLElement;
}

/** The routing row for an operation, found by the operation name it renders. */
function operationRow(operationName: string): HTMLElement {
  const row = within(routingTable()).getByText(operationName).closest('tr');
  if (!row) throw new Error(`No operations row for ${operationName}`);
  return row as HTMLElement;
}

/**
 * That row's office Complete button. `queryBy` because a COMPLETE operation
 * renders "Done" instead, and one test asserts exactly that.
 */
function completeButton(operationName: string): HTMLElement | null {
  return within(operationRow(operationName)).queryByRole('button', { name: 'Complete' });
}

/** The same button, asserted present — the shape most tests below want. */
function requireCompleteButton(operationName: string): HTMLElement {
  const button = completeButton(operationName);
  if (!button) throw new Error(`No Complete button in the ${operationName} row`);
  return button;
}

/**
 * Rejection shaped the way the Axios interceptor hands one to a `.catch`:
 * `detail` already collapsed to a string by `normalizeAxiosErrorDetail`.
 */
function httpError(status: number, detail: string) {
  return { response: { status, data: { detail } } };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'admin', is_superuser: false };

  mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder());
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getAIRecommendations.mockResolvedValue([]);
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.updateWorkOrder.mockResolvedValue({});
});

describe('Sequential operations switch — what it renders', () => {
  it('reads OFF, with the pooled helper line, on a pooled work order', async () => {
    const toggle = await renderAndFindSwitch();

    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(POOLED_HELP)).toBeInTheDocument();
    expect(screen.queryByText(SEQUENCED_HELP)).not.toBeInTheDocument();
  });

  it('reads ON, with the sequenced helper line, on a sequenced work order', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ sequential_operations: true, operations: SEQUENCED_OPERATIONS })
    );

    const toggle = await renderAndFindSwitch();

    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText(SEQUENCED_HELP)).toBeInTheDocument();
    expect(screen.queryByText(POOLED_HELP)).not.toBeInTheDocument();
  });

  it('reads an ABSENT flag as pooled, not sequenced', async () => {
    // A response from an API that predates the column, or a row created before
    // migration 081 ran. Pooled is what such a work order actually behaves as,
    // so showing it as sequenced would be a lie about the floor's rules.
    const withoutFlag = makeWorkOrder();
    // `delete` rather than a rest-destructure: the discarded binding is an
    // unused var, and CI lints with --max-warnings=0.
    delete withoutFlag.sequential_operations;
    mockedApi.getWorkOrder.mockResolvedValue(withoutFlag);

    expect(await renderAndFindSwitch()).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(POOLED_HELP)).toBeInTheDocument();
  });

  it('describes the switch with its helper line for screen readers', async () => {
    const toggle = await renderAndFindSwitch();
    const describedBy = toggle.getAttribute('aria-describedby');

    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)).toHaveTextContent(POOLED_HELP);
  });
});

describe('Sequential operations switch — the write', () => {
  it('turns sequencing ON with the version in the payload, and re-reads the work order', async () => {
    // The server's answer to the flip: promotion re-ran, so 20/30 fell back to
    // PENDING. The page must show THAT, not the four rows it already had.
    mockedApi.getWorkOrder
      .mockResolvedValueOnce(makeWorkOrder())
      .mockResolvedValue(
        makeWorkOrder({
          version: 4,
          sequential_operations: true,
          operations: SEQUENCED_OPERATIONS,
        })
      );

    const user = userEvent.setup();
    await user.click(await renderAndFindSwitch());

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      sequential_operations: true,
      // Optimistic lock — the endpoint 409s a stale version.
      version: 3,
    });

    expect(
      await screen.findByText(
        'Operations now run in sequence — each one unlocks when the previous is complete'
      )
    ).toBeInTheDocument();

    // Property 2: the Status column reflects the REFETCH. Under the pool three
    // operations read "ready"; under the routing exactly one does.
    await waitFor(() => {
      expect(within(routingTable()).getAllByText('ready')).toHaveLength(1);
    });
    expect(within(routingTable()).getAllByText('pending')).toHaveLength(3);
    await waitFor(() => expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true'));
  });

  it('turns sequencing OFF again, back to the dispatch pool', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ sequential_operations: true, operations: SEQUENCED_OPERATIONS })
    );

    const user = userEvent.setup();
    await user.click(await renderAndFindSwitch());

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      sequential_operations: false,
      version: 3,
    });
    expect(
      await screen.findByText('Operations at the same work center can now run in any order')
    ).toBeInTheDocument();
  });

  it('shows a pending state and refuses a second click while the write is in flight', async () => {
    let release: (value: unknown) => void = () => undefined;
    mockedApi.updateWorkOrder.mockImplementation(
      () => new Promise((resolve) => { release = resolve; })
    );

    const user = userEvent.setup();
    const toggle = await renderAndFindSwitch();
    await user.click(toggle);

    await waitFor(() => expect(toggle).toBeDisabled());
    // Double-click guard: the button is disabled, so this lands nowhere.
    await user.click(toggle);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);

    await act(async () => {
      release({});
    });
    await waitFor(() => expect(screen.getByRole('switch')).not.toBeDisabled());
  });
});

describe('Sequential operations switch — refused writes stay non-optimistic', () => {
  it('leaves the switch OFF and shows the 409 detail verbatim on a stale version', async () => {
    const detail = 'Work order was modified by someone else. Reload and try again.';
    mockedApi.updateWorkOrder.mockRejectedValue(httpError(409, detail));

    const user = userEvent.setup();
    const toggle = await renderAndFindSwitch();
    await user.click(toggle);

    expect(await screen.findByText(detail)).toBeInTheDocument();
    // The whole point: the UI never shows a state the server refused.
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(POOLED_HELP)).toBeInTheDocument();
    expect(screen.queryByText(SEQUENCED_HELP)).not.toBeInTheDocument();
    // The three weld operations are still pooled, because nothing changed.
    expect(within(routingTable()).getAllByText('ready')).toHaveLength(3);
    // A 409 refetches so the next attempt carries a fresh version.
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(1));
  });

  it('leaves the switch ON and shows the 403 detail verbatim when the role is refused', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ sequential_operations: true, operations: SEQUENCED_OPERATIONS })
    );
    mockedApi.updateWorkOrder.mockRejectedValue(
      httpError(403, 'Not authorized to perform this action')
    );

    const user = userEvent.setup();
    await user.click(await renderAndFindSwitch());

    expect(await screen.findByText('Not authorized to perform this action')).toBeInTheDocument();
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText(SEQUENCED_HELP)).toBeInTheDocument();
  });

  it('falls back to a plain message when the server sends no usable detail', async () => {
    mockedApi.updateWorkOrder.mockRejectedValue(new Error('Network Error'));

    const user = userEvent.setup();
    await user.click(await renderAndFindSwitch());

    expect(await screen.findByText('Failed to change operation sequencing')).toBeInTheDocument();
    expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
  });
});

describe('Sequential operations switch — laser work orders', () => {
  it('renders no switch on a laser_cutting work order, and says why', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ work_order_type: 'laser_cutting', part_id: null })
    );
    renderDetail();

    await screen.findByRole('heading', { name: 'Operations / Routing' });

    expect(screen.queryByRole('switch', { name: 'Sequential operations' })).not.toBeInTheDocument();
    expect(screen.queryByText(POOLED_HELP)).not.toBeInTheDocument();
    expect(screen.queryByText(SEQUENCED_HELP)).not.toBeInTheDocument();
    expect(
      screen.getByText(/Nest work orders are always pooled/)
    ).toBeInTheDocument();
  });

  it('still renders no switch on a laser work order whose flag happens to be true', async () => {
    // The backend ignores the column on a nest WO, so its stored value must not
    // resurrect a control that cannot change anything.
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ work_order_type: 'laser_cutting', part_id: null, sequential_operations: true })
    );
    renderDetail();

    await screen.findByRole('heading', { name: 'Operations / Routing' });
    expect(screen.queryByRole('switch', { name: 'Sequential operations' })).not.toBeInTheDocument();
  });
});

describe('Sequential operations switch — role gate', () => {
  it.each(['operator', 'viewer', 'quality'])(
    'offers %s no switch (no work_orders:edit) but still shows the mode',
    async (role) => {
      mockUser = { id: 2, role, is_superuser: false };
      renderDetail();

      await screen.findByRole('heading', { name: 'Operations / Routing' });

      expect(screen.queryByRole('switch', { name: 'Sequential operations' })).not.toBeInTheDocument();
      // Reading the Status column requires knowing WHICH rule is in force, so
      // the mode is shown even where the control is not.
      expect(screen.getByText(POOLED_HELP)).toBeInTheDocument();
      expect(screen.getByText('Off')).toBeInTheDocument();
    }
  );

  it.each(['admin', 'manager', 'supervisor'])('offers %s the switch', async (role) => {
    mockUser = { id: 3, role, is_superuser: false };
    renderDetail();

    expect(await findSwitch()).toBeInTheDocument();
  });

  it('offers a superuser the switch regardless of role', async () => {
    mockUser = { id: 4, role: 'viewer', is_superuser: true };
    renderDetail();

    expect(await findSwitch()).toBeInTheDocument();
  });
});

describe('Sequenced routing — the office Complete button matches the server gate', () => {
  beforeEach(() => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ sequential_operations: true, operations: SEQUENCED_OPERATIONS })
    );
  });

  it('offers Complete on operation 10 only, and disables 20/30/40 behind it', async () => {
    await renderAndFindSwitch();

    // 10 is the lowest incomplete operation: nothing sits below it, so the
    // server would accept it and the button stays live.
    expect(requireCompleteButton('Skid Fit')).toBeEnabled();

    // 20 and 30 share work center 9 with it. Under sequencing the same-work-
    // center waiver does NOT apply (`work_order_allows_same_work_center` is
    // false), so both are refused — this pair IS the reported defect.
    expect(requireCompleteButton('Wall Fit Up')).toBeDisabled();
    expect(requireCompleteButton('Accessory Fit Up')).toBeDisabled();

    // 40 is on a DIFFERENT work center and is equally refused: the rule is
    // "any lower sequence not complete", never a per-cell question.
    expect(requireCompleteButton('Weld Out')).toBeDisabled();
  });

  it('moves the offer to 20 once 10 is complete, and still refuses 30 and 40', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({
        sequential_operations: true,
        operations: SEQUENCED_OPERATIONS_10_COMPLETE,
      })
    );

    await renderAndFindSwitch();

    // A complete operation has no Complete button at all — it reads "Done".
    expect(completeButton('Skid Fit')).toBeNull();
    expect(within(operationRow('Skid Fit')).getByText('Done')).toBeInTheDocument();

    expect(requireCompleteButton('Wall Fit Up')).toBeEnabled();
    expect(requireCompleteButton('Accessory Fit Up')).toBeDisabled();
    expect(requireCompleteButton('Weld Out')).toBeDisabled();
  });

  it('names the blocking operation on hover and keeps the button’s accessible name', async () => {
    await renderAndFindSwitch();

    const blocked = requireCompleteButton('Wall Fit Up');
    // The server's own wording, plus WHICH operation is in the way — a disabled
    // control that cannot say why reads as a broken page.
    expect(blocked).toHaveAttribute(
      'title',
      expect.stringContaining('Previous operations must be completed first')
    );
    expect(blocked).toHaveAttribute('title', expect.stringContaining('operation 10 (Skid Fit)'));
    // jsx-a11y is enforced with --max-warnings=0: disabling a control must not
    // cost it its name, and the reason lives in `title`, not in place of the label.
    expect(blocked).toHaveAccessibleName('Complete');

    // The live one carries the plain label, not a leaked explanation.
    expect(requireCompleteButton('Skid Fit')).toHaveAttribute('title', 'Complete Operation');
  });

  it('opens no completion modal from a blocked row, and still opens one from the live row', async () => {
    const user = userEvent.setup();
    await renderAndFindSwitch();

    await user.click(requireCompleteButton('Accessory Fit Up'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // The gate must not have disabled the page: the operation the server would
    // accept still reaches the quantity/scrap modal.
    await user.click(requireCompleteButton('Skid Fit'));
    expect(
      await screen.findByRole('heading', { name: 'Complete operation "Skid Fit"' })
    ).toBeInTheDocument();
  });
});

describe('Pooled work orders — the Complete button is deliberately left alone', () => {
  it('keeps every operation’s Complete button live under the dispatch pool', async () => {
    // Today's behavior, preserved byte for byte. The pooled rule waives a
    // same-work-center predecessor EXCEPT an ON_HOLD one, and the failure modes
    // are not symmetric: disabling a control the server would have ACCEPTED
    // hides a legal action with no override, which is worse than the 400.
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ sequential_operations: false, operations: POOLED_OPERATIONS })
    );

    await renderAndFindSwitch();

    for (const name of ['Skid Fit', 'Wall Fit Up', 'Accessory Fit Up']) {
      expect(requireCompleteButton(name)).toBeEnabled();
      expect(requireCompleteButton(name)).toHaveAttribute('title', 'Complete Operation');
    }
    // Including the cross-work-center one the pooled server rule WOULD refuse:
    // gating it is a behavior change this defect fix deliberately does not make.
    expect(requireCompleteButton('Weld Out')).toBeEnabled();
  });

  it('leaves an absent flag pooled — an ungated table, not a gated one', async () => {
    const withoutFlag = makeWorkOrder({ operations: SEQUENCED_OPERATIONS });
    delete withoutFlag.sequential_operations;
    mockedApi.getWorkOrder.mockResolvedValue(withoutFlag);
    renderDetail();

    await screen.findByRole('heading', { name: 'Operations / Routing' });

    expect(requireCompleteButton('Wall Fit Up')).toBeEnabled();
    expect(requireCompleteButton('Weld Out')).toBeEnabled();
  });
});

describe('Laser work orders — never gated, whatever the column says', () => {
  it('leaves every nest operation completable even with sequential_operations true', async () => {
    // `is_laser_dispatch_work_order` short-circuits ABOVE the flag at every
    // backend seam and drops predecessor gating entirely, so a client gate here
    // would refuse work the server accepts — the worse direction of the mismatch.
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({
        work_order_type: 'laser_cutting',
        part_id: null,
        sequential_operations: true,
        operations: SEQUENCED_OPERATIONS,
      })
    );
    renderDetail();

    await screen.findByRole('heading', { name: 'Operations / Routing' });

    for (const name of ['Skid Fit', 'Wall Fit Up', 'Accessory Fit Up', 'Weld Out']) {
      expect(requireCompleteButton(name)).toBeEnabled();
    }
  });
});
