/**
 * WorkOrderDetail — inline "Notes & Instructions" edit, and the LOST-UPDATE
 * GUARD it now shares with the page's other inline editor (the due-date pencil
 * in the Due Date tile).
 *
 * The panel used to be read-only. It now carries a header Edit control that
 * swaps in two textareas (Notes / Special Instructions) plus Save + Cancel.
 *
 * Why the due-date conflict cases live in this file rather than beside the
 * due-date happy path in WorkOrderDetail.nestDispatch.test.tsx: after the
 * refactor the two editors do not have two concurrency behaviors, they have
 * ONE — a single `saveWorkOrderPatch` writer, a single `fieldConflict`
 * discriminated union, a single <ConfirmDialog>, and a single
 * `handleConflictReplace` switching on `kind`. Tests for one mechanism belong
 * in one file, so a change that fixes one editor's half and breaks the other's
 * goes red side by side instead of in a suite nobody re-read. It also keeps
 * `pushConcurrentChange` — which drives the change through the page's real
 * websocket handler and its 500ms debounce — to exactly one definition;
 * nestDispatch.test.tsx has no fake-timer harness and is about laser dispatch.
 *
 * The property this file exists to pin down is STATUS-INDEPENDENCE: the editor
 * is offered at EVERY work-order status — draft, released, in_progress,
 * on_hold, complete, closed, cancelled. That is deliberate, not an oversight.
 * `PUT /work-orders/{id}` carries no status gate on these two fields (its only
 * 409s are status TRANSITIONS), and the shop-floor instruction worth writing
 * down is usually learned AFTER release. A note is documentation, not
 * production record: it moves no stock and completes no operation. So if
 * someone later adds `workOrder.status === 'draft' &&` to the gate, the
 * parameterized test below must go red rather than silently shipping a control
 * that hides an operation the API allows.
 *
 * Also covered: the exact updateWorkOrder payload (incl. `version` for the
 * optimistic lock and null-on-empty), the work_orders:edit role gate, the
 * NON-optimistic refetch, the 409 branch (refetch + editor stays open holding
 * the draft), the dirty-Cancel confirm gate, and — in the last describe — the
 * due-date half of the shared guard. The due-date happy path, its 409, cancel
 * and role gate stay in nestDispatch.test.tsx; they are not duplicated here.
 *
 * Harness mirrors WorkOrderDetail.nestDispatch.test.tsx (side-channels mocked,
 * real ToastProvider so toast text is assertable).
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { useWebSocket } from '../hooks/useWebSocket';
import { WorkOrder, WorkOrderStatus } from '../types';

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

const NOTES = 'Original planning note';
const INSTRUCTIONS = 'Original instructions';

/** Plain production WO, RELEASED — the user's motivating case. */
function makeWorkOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 42,
    version: 3,
    work_order_number: 'WO-0042',
    part_id: 100,
    work_order_type: 'production',
    quantity_ordered: 10,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 8,
    actual_hours: 0,
    notes: NOTES,
    special_instructions: INSTRUCTIONS,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    operations: [],
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

/** Renders, waits for the WO, and opens the inline editor. */
async function openNotesEditor() {
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
  return screen.getByLabelText('Notes') as HTMLTextAreaElement;
}

/**
 * Lands a concurrent server-side change the way production would: through the
 * page's own `onMessage` handler (captured off the mocked useWebSocket), which
 * debounces 500ms in `scheduleRealtimeRefresh` before refetching — hence the
 * fake timers its callers install.
 *
 * `next` is the WHOLE work order the server now holds, not a delta, so a caller
 * whose guard is keyed on a field must restate that field when it is meant to
 * be unchanged. That is deliberate: "the due date is still 2026-07-25" should
 * be visible in the test that depends on it.
 *
 * Shared by both editors' guard tests — one definition, because after the
 * refactor there is one guard.
 */
async function pushConcurrentChange(next: Partial<WorkOrder>) {
  mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder(next));
  const options = jest.mocked(useWebSocket).mock.calls.at(-1)?.[0];
  expect(options?.onMessage).toBeDefined();

  await act(async () => {
    options?.onMessage?.(
      { type: 'work_order_update', data: { work_order_id: 42 } },
      new MessageEvent('message')
    );
    // scheduleRealtimeRefresh debounces before calling loadWorkOrder().
    await jest.advanceTimersByTimeAsync(500);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.restoreAllMocks();
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
  mockedApi.updateWorkOrder.mockResolvedValue({});
});

describe('WorkOrderDetail inline notes edit — save path', () => {
  it('opens seeded from the server values, with the 2000-char cap and counts', async () => {
    const notes = await openNotesEditor();
    const instructions = screen.getByLabelText('Special Instructions') as HTMLTextAreaElement;

    expect(notes).toHaveValue(NOTES);
    expect(instructions).toHaveValue(INSTRUCTIONS);
    // Cap mirrors WorkOrderUpdate.notes / .special_instructions (max_length=2000)
    // so an over-long note is refused in the browser, not as a raw 422.
    expect(notes).toHaveAttribute('maxlength', '2000');
    expect(instructions).toHaveAttribute('maxlength', '2000');
    // Distinct lengths (22 vs 21) so each count line is unambiguous.
    expect(screen.getByText(`${NOTES.length} / 2000`)).toBeInTheDocument();
    expect(screen.getByText(`${INSTRUCTIONS.length} / 2000`)).toBeInTheDocument();
  });

  it('saves a RELEASED work order with the exact payload: trimmed notes, null-on-empty, and the version', async () => {
    const notes = await openNotesEditor();
    const instructions = screen.getByLabelText('Special Instructions');
    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;

    // Surrounding whitespace is trimmed off...
    fireEvent.change(notes, {
      target: { value: '  Item 80 uses R.375, not the usual R.19  ' },
    });
    // ...and a field emptied to whitespace goes over as null, not '' — "cleared"
    // must read as absent downstream (traveler print, kiosk), not present-but-blank.
    fireEvent.change(instructions, { target: { value: '   ' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      notes: 'Item 80 uses R.375, not the usual R.19',
      special_instructions: null,
      version: 3,
    });
    expect(await screen.findByText('Notes updated')).toBeInTheDocument();
    // Editor closes and the page re-reads the WO.
    await waitFor(() => expect(screen.queryByLabelText('Notes')).not.toBeInTheDocument());
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBefore));
  });

  it('sends null for BOTH fields when both are cleared', async () => {
    const notes = await openNotesEditor();

    fireEvent.change(notes, { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Special Instructions'), { target: { value: '' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      notes: null,
      special_instructions: null,
      version: 3,
    });
  });

  it('is NON-optimistic: the panel renders what the refetch returns, not what was typed', async () => {
    mockedApi.getWorkOrder
      .mockResolvedValueOnce(makeWorkOrder())
      .mockResolvedValue(makeWorkOrder({ version: 4, notes: 'Server copy of the note' }));

    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'What the planner typed' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(await screen.findByText('Server copy of the note')).toBeInTheDocument();
    expect(screen.queryByText('What the planner typed')).not.toBeInTheDocument();
  });

  it('renders a multi-line note with newlines preserved (pre-wrap)', async () => {
    // jsdom applies no stylesheet, so the class is the only observable handle on
    // the one behavior that matters here: the editor accepts newlines, so a
    // multi-line note has to render as typed rather than as one run-on line.
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({ notes: 'Line one\nLine two' })
    );
    renderDetail();

    const rendered = await screen.findByText(/Line one/);
    expect(rendered).toHaveTextContent('Line one Line two'); // normalized by RTL
    expect(rendered.textContent).toBe('Line one\nLine two');
    expect(rendered).toHaveClass('whitespace-pre-wrap');
  });
});

describe('WorkOrderDetail inline notes edit — status independence', () => {
  // The central property. A note is documentation, not production record, and
  // the server gates none of these — so a future status gate must fail here.
  const STATUSES: WorkOrderStatus[] = [
    'draft',
    'released',
    'in_progress',
    'on_hold',
    'complete',
    'closed',
    'cancelled',
  ];

  it.each(STATUSES)('offers the Edit control on a %s work order', async (status) => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ status }));
    renderDetail();

    await screen.findByRole('heading', { name: 'WO-0042' });
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });

  it.each(STATUSES)('saves notes on a %s work order (no status gate on the write)', async (status) => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ status }));
    const notes = await openNotesEditor();

    fireEvent.change(notes, { target: { value: `note written at ${status}` } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      notes: `note written at ${status}`,
      special_instructions: INSTRUCTIONS,
      version: 3,
    });
  });
});

describe('WorkOrderDetail inline notes edit — role gate', () => {
  it.each(['operator', 'viewer'])(
    'hides the Edit control from %s (no work_orders:edit)',
    async (role) => {
      mockUser = { id: 2, role, is_superuser: false };
      renderDetail();

      await screen.findByRole('heading', { name: 'WO-0042' });
      expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
      // The panel itself still renders — the gate is on editing, not reading.
      expect(screen.getByText(NOTES)).toBeInTheDocument();
    }
  );

  it('offers it to a superuser whose role alone would not carry work_orders:edit', async () => {
    mockUser = { id: 3, role: 'viewer', is_superuser: true };
    renderDetail();

    expect(await screen.findByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });
});

describe('WorkOrderDetail inline notes edit — failure paths', () => {
  it('on a 409 conflict: verbatim detail toast, refetch, and the editor stays open with the draft', async () => {
    const conflict = 'Work order was modified by someone else. Refresh and try again.';
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: conflict } },
    });

    const notes = await openNotesEditor();
    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;
    fireEvent.change(notes, { target: { value: 'Bevel the far edge before forming' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    // The server's refusal surfaces verbatim...
    expect(await screen.findByText(conflict)).toBeInTheDocument();
    expect(screen.queryByText('Notes updated')).not.toBeInTheDocument();
    // ...the 409 branch self-heals so the NEXT save carries a fresh version...
    await waitFor(() => expect(mockedApi.getWorkOrder.mock.calls.length).toBeGreaterThan(loadsBefore));
    // ...and the retry doesn't cost the planner their typing.
    expect(screen.getByLabelText('Notes')).toHaveValue('Bevel the far edge before forming');
    expect(screen.getByLabelText('Special Instructions')).toHaveValue(INSTRUCTIONS);
  });

  it('on a non-409 failure: verbatim detail toast, editor open, and NO refetch', async () => {
    const refusal = 'Notes must be 2000 characters or fewer';
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 422, data: { detail: refusal } },
    });

    const notes = await openNotesEditor();
    const loadsBefore = mockedApi.getWorkOrder.mock.calls.length;
    fireEvent.change(notes, { target: { value: 'too long, allegedly' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(await screen.findByText(refusal)).toBeInTheDocument();
    expect(mockedApi.getWorkOrder.mock.calls.length).toBe(loadsBefore);
    expect(screen.getByLabelText('Notes')).toHaveValue('too long, allegedly');
  });

  it('falls back to a generic message when the server sends no usable detail', async () => {
    mockedApi.updateWorkOrder.mockRejectedValue(new Error('network down'));

    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'anything' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(await screen.findByText('Failed to update notes')).toBeInTheDocument();
    expect(screen.getByLabelText('Notes')).toBeInTheDocument();
  });
});

describe('WorkOrderDetail inline notes edit — lost-update guard', () => {
  // The optimistic-lock `version` does NOT protect this editor. PUT
  // /work-orders/{id} broadcasts a work_order update, this page refetches on
  // that broadcast, so a concurrent planner's note edit silently advances
  // `workOrder.version` under the open editor and the next Save returns a clean
  // 200 having erased text this user never saw. The guard compares the note
  // TEXT against the baseline captured when the editor opened.
  //
  // These tests drive the change through the REAL mechanism: the page's own
  // `onMessage` handler, via the module-level `pushConcurrentChange` above,
  // which debounces 500ms in `scheduleRealtimeRefresh` — hence fake timers.
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  async function clickSave() {
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });
  }

  it('refuses the save and opens the dialog when the note changed under the editor', async () => {
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft' } });

    await pushConcurrentChange({ version: 7, notes: 'someone else got here first' });
    await clickSave();

    // The whole point: no write went out.
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Notes changed by someone else')).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Someone else changed the notes on WO-0042 while you were editing\./)
    ).toBeInTheDocument();
    // No false success.
    expect(screen.queryByText('Notes updated')).not.toBeInTheDocument();
  });

  it('"Replace with mine" writes the user draft with the CURRENT version', async () => {
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft wins' } });

    // The concurrent write also bumped the row version 3 -> 7.
    await pushConcurrentChange({ version: 7, notes: 'someone else got here first' });
    await clickSave();
    await screen.findByRole('dialog');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      notes: 'my draft wins',
      special_instructions: INSTRUCTIONS,
      version: 7,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText('Notes updated')).toBeInTheDocument();
  });

  it('"Keep editing" makes no API call and leaves both drafts intact', async () => {
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft' } });
    fireEvent.change(screen.getByLabelText('Special Instructions'), {
      target: { value: 'my instructions' },
    });

    await pushConcurrentChange({ version: 7, notes: 'someone else got here first' });
    await clickSave();
    await screen.findByRole('dialog');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
    });

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Notes')).toHaveValue('my draft');
    expect(screen.getByLabelText('Special Instructions')).toHaveValue('my instructions');
  });

  it.each([
    ['notes only', { notes: 'theirs' }, /changed the notes on WO-0042/],
    [
      'special instructions only',
      { special_instructions: 'theirs' },
      /changed the special instructions on WO-0042/,
    ],
    [
      'both fields',
      { notes: 'theirs', special_instructions: 'theirs too' },
      /changed the notes and special instructions on WO-0042/,
    ],
  ] as const)('names the changed field(s) — %s', async (_label, change, expected) => {
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft' } });

    await pushConcurrentChange({ version: 7, ...change });
    await clickSave();

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(expected)).toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });

  it('does not fire on an unrelated concurrent change (a status move, notes untouched)', async () => {
    // Keyed on the note TEXT, not `version` — WorkOrder maps version_id_col, so
    // every operation completion bumps it and a version-keyed check would make
    // notes unsavable on any live job.
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft' } });

    await pushConcurrentChange({ version: 9, status: 'in_progress', quantity_complete: 4 });
    await clickSave();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      notes: 'my draft',
      special_instructions: INSTRUCTIONS,
      version: 9,
    });
  });

  it('saves directly with no dialog when nothing moved', async () => {
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft' } });

    await clickSave();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      notes: 'my draft',
      special_instructions: INSTRUCTIONS,
      version: 3,
    });
  });

  it('re-triggers on a SECOND concurrent change after a Replace whose write failed', async () => {
    // The approval covers the change the user was shown, not every future one.
    const notes = await openNotesEditor();
    fireEvent.change(notes, { target: { value: 'my draft' } });

    await pushConcurrentChange({ version: 7, notes: 'first concurrent edit' });
    await clickSave();
    await screen.findByRole('dialog');

    // Replace, but the write is refused — the editor stays open and the dialog
    // closes, with 'first concurrent edit' adopted as the new baseline.
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Stale work order version' } },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });
    expect(await screen.findByText('Stale work order version')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Notes')).toHaveValue('my draft');

    // A FURTHER edit lands. The next Save must ask again, not ride through on
    // the first approval.
    mockedApi.updateWorkOrder.mockResolvedValue({});
    await pushConcurrentChange({ version: 8, notes: 'second concurrent edit' });
    // Counted, not `not.toHaveBeenCalled()` — the refused Replace above already
    // put one call on the wire.
    const writesBefore = mockedApi.updateWorkOrder.mock.calls.length;
    await clickSave();

    expect(mockedApi.updateWorkOrder.mock.calls.length).toBe(writesBefore);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/changed the notes on WO-0042/)).toBeInTheDocument();
  });
});

describe('WorkOrderDetail inline notes edit — work-order change', () => {
  /** Drives a client-side :id change WITHOUT remounting WorkOrderDetail. */
  function NavProbe() {
    const navigate = useNavigate();
    return (
      <button type="button" onClick={() => navigate('/work-orders/43')}>
        go to next WO
      </button>
    );
  }

  it('closes the editor on a work-order change so a stale draft cannot be saved onto the next WO', async () => {
    // The route keeps this component mounted across an :id change, so an editor
    // left open would carry WO-0042's draft onto WO-0043 — and Save would write
    // it there, against the wrong record.
    mockedApi.getWorkOrder.mockImplementation(async (woId: number) =>
      woId === 43
        ? makeWorkOrder({ id: 43, work_order_number: 'WO-0043', notes: 'A different order' })
        : makeWorkOrder()
    );

    render(
      <ToastProvider>
        <MemoryRouter initialEntries={['/work-orders/42']}>
          <NavProbe />
          <Routes>
            <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Notes'), { target: { value: 'meant for WO-0042' } });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'go to next WO' }));
    });

    expect(await screen.findByRole('heading', { name: 'WO-0043' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Notes')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('meant for WO-0042')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
  });
});

describe('WorkOrderDetail inline notes edit — cancel / unsaved-changes gate', () => {
  it('prompts on a dirty draft and keeps the editor open when the user declines', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const notes = await openNotesEditor();

    fireEvent.change(notes, { target: { value: 'half-typed note' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(confirmSpy).toHaveBeenCalledWith('You have unsaved note changes. Discard them?');
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Notes')).toHaveValue('half-typed note');
  });

  it('discards and closes when the user accepts the prompt — still no API call', async () => {
    jest.spyOn(window, 'confirm').mockReturnValue(true);
    const notes = await openNotesEditor();

    fireEvent.change(notes, { target: { value: 'half-typed note' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByLabelText('Notes')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    // Read mode shows the untouched server value.
    expect(screen.getByText(NOTES)).toBeInTheDocument();
  });

  it('does not prompt when nothing was typed', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openNotesEditor();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Notes')).not.toBeInTheDocument();
  });

  it('does not prompt when the draft is typed back to the saved text', async () => {
    // notesDirty compares against the CURRENT server values, not a snapshot
    // taken at open — so an edit-and-undo is clean, not dirty.
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const notes = await openNotesEditor();

    fireEvent.change(notes, { target: { value: 'scratch that' } });
    fireEvent.change(notes, { target: { value: NOTES } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Notes')).not.toBeInTheDocument();
  });
});

describe('WorkOrderDetail inline due-date edit — lost-update guard', () => {
  // The other half of the shared guard. Same failure mode as notes and the same
  // reason `version` cannot carry it: PUT /work-orders/{id} broadcasts a
  // work_order update, this page refetches on that broadcast, so a concurrent
  // reschedule silently advances `workOrder.version` under the open editor and
  // the next Save returns a clean 200 having erased it. A due date is the
  // promise date feeding OTD/OTIF, so losing someone's change is not cosmetic —
  // it re-dates a commitment to a customer with nothing on screen saying so.
  //
  // Keyed on the DATE, not on `version`: WorkOrder maps version_id_col, so every
  // operation completion bumps it and a version-keyed check would make the due
  // date unsettable on any live job.
  //
  // The happy path, the 409, cancel and the role gate for this editor live in
  // WorkOrderDetail.nestDispatch.test.tsx and are deliberately not repeated.
  const DUE_DATE = '2026-07-25'; // what the server holds when the editor opens
  const MY_DRAFT = '2026-08-01'; // what this user types
  const THEIR_DATE = '2026-09-30'; // what a concurrent planner sets meanwhile

  beforeEach(() => {
    jest.useFakeTimers();
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ due_date: DUE_DATE }));
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  /** Renders, waits for the WO, opens the due-date pencil, returns the input. */
  async function openDueDateEditor() {
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit due date' }));
    return screen.getByLabelText('Due date') as HTMLInputElement;
  }

  async function clickSaveDueDate() {
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save due date' }));
    });
  }

  it('refuses the save and opens the dialog when the due date changed under the editor', async () => {
    const input = await openDueDateEditor();
    expect(input).toHaveValue(DUE_DATE);
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, due_date: THEIR_DATE });
    await clickSaveDueDate();

    // The whole point: no write went out.
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    // Titled for THIS editor — the shared dialog must not tell a planner
    // rescheduling a job that their notes changed.
    expect(within(dialog).getByText('Due date changed by someone else')).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Someone else changed the due date on WO-0042 while you were editing\./)
    ).toBeInTheDocument();
    // No false success.
    expect(screen.queryByText(/^Due date set to/)).not.toBeInTheDocument();
  });

  it('"Replace with mine" writes the user draft at the version the concurrent write bumped it to', async () => {
    const input = await openDueDateEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    // The concurrent write also bumped the row version 3 -> 7.
    await pushConcurrentChange({ version: 7, due_date: THEIR_DATE });
    await clickSaveDueDate();
    await screen.findByRole('dialog');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      due_date: MY_DRAFT,
      version: 7,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText('Due date set to Aug 1, 2026')).toBeInTheDocument();
  });

  it('"Keep editing" makes no API call, closes the dialog, and leaves the date draft intact', async () => {
    const input = await openDueDateEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, due_date: THEIR_DATE });
    await clickSaveDueDate();
    await screen.findByRole('dialog');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
    });

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // The editor is still open holding what they typed — "keep editing" has to
    // mean keep editing, not discard and reopen.
    expect(screen.getByLabelText('Due date')).toHaveValue(MY_DRAFT);
  });

  it('does not fire on an unrelated concurrent change (version bumped, due date untouched)', async () => {
    // The design decision this pins: the guard is keyed on the DUE DATE, so a
    // job that is simply running — operations completing, each bumping
    // version_id_col — must stay reschedulable. A version-keyed check would put
    // this dialog in front of the planner on every live work order.
    const input = await openDueDateEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({
      version: 9,
      due_date: DUE_DATE, // restated: the server did NOT move the date
      status: 'in_progress',
      quantity_complete: 4,
    });
    await clickSaveDueDate();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      due_date: MY_DRAFT,
      version: 9,
    });
    expect(await screen.findByText('Due date set to Aug 1, 2026')).toBeInTheDocument();
  });

  it('clears the due date (empty draft -> null) without mistaking the clear for a conflict', async () => {
    // The guard compares SERVER state to the baseline, never the draft to the
    // baseline — an emptied input is this user's own edit, not someone else's.
    const input = await openDueDateEditor();
    fireEvent.change(input, { target: { value: '' } });

    await clickSaveDueDate();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      // null, not '' — "no due date" must read as absent everywhere downstream,
      // not as a present-but-blank promise date.
      due_date: null,
      version: 3,
    });
    expect(await screen.findByText('Due date cleared')).toBeInTheDocument();
  });

  it('adopts the replaced date as the new baseline, so a retry after a refused Replace does not re-ask', async () => {
    // Confirming an overwrite covers the change the user was SHOWN. Once
    // approved, that same change must not keep interrupting them — otherwise a
    // server refusal on an unrelated field (a 422, which does not refetch)
    // leaves them re-confirming a decision they already made.
    const input = await openDueDateEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, due_date: THEIR_DATE });
    await clickSaveDueDate();
    await screen.findByRole('dialog');

    const refusal = 'Due date cannot precede the release date';
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 422, data: { detail: refusal } },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });
    expect(await screen.findByText(refusal)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Due date')).toHaveValue(MY_DRAFT);

    // Retry with nothing further changed server-side: straight through.
    mockedApi.updateWorkOrder.mockResolvedValue({});
    const writesBefore = mockedApi.updateWorkOrder.mock.calls.length;
    await clickSaveDueDate();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder.mock.calls.length).toBe(writesBefore + 1);
    expect(mockedApi.updateWorkOrder).toHaveBeenLastCalledWith(42, {
      due_date: MY_DRAFT,
      version: 7,
    });
  });

  it('re-triggers on a SECOND concurrent reschedule after a Replace whose write failed', async () => {
    // The flip side of the test above: the approval covers the change shown,
    // not every future one.
    const input = await openDueDateEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, due_date: THEIR_DATE });
    await clickSaveDueDate();
    await screen.findByRole('dialog');

    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Stale work order version' } },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });
    expect(await screen.findByText('Stale work order version')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // A FURTHER reschedule lands. The next Save must ask again.
    mockedApi.updateWorkOrder.mockResolvedValue({});
    await pushConcurrentChange({ version: 8, due_date: '2026-10-15' });
    // Counted, not `not.toHaveBeenCalled()` — the refused Replace above already
    // put one call on the wire.
    const writesBefore = mockedApi.updateWorkOrder.mock.calls.length;
    await clickSaveDueDate();

    expect(mockedApi.updateWorkOrder.mock.calls.length).toBe(writesBefore);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Due date changed by someone else')).toBeInTheDocument();
    expect(within(dialog).getByText(/changed the due date on WO-0042/)).toBeInTheDocument();
  });
});
