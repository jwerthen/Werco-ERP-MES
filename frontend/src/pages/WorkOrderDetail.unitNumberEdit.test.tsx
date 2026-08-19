/**
 * WorkOrderDetail — the inline "Unit #" editor and its lost-update guard.
 *
 * Unit assignment routinely happens AFTER the work order is raised, so the field has to
 * be editable rather than create-only. That makes it the third editor on this page
 * sharing one `fieldConflict` dialog, and it inherits the same guard as notes and the
 * due date for the same reason: `PUT /work-orders/{id}` broadcasts a work_order update,
 * this page refetches on that broadcast, so a concurrent edit silently advances
 * `workOrder.version` under an open editor and the next Save returns a clean 200 having
 * erased somebody's change. `version` therefore cannot carry the guard — the guard is
 * keyed on the FIELD's server value.
 *
 * Why this field in particular needs it: the unit number is the identity of the physical
 * assembly on the bench, and it is on the kiosk hero, the crew station and the public TV
 * wall. Silently overwriting a corrected unit re-labels a job the floor is already
 * looking at, with nothing on screen saying so.
 *
 * What is pinned here:
 *  - the save sends `{ unit_number, version }` and nothing else;
 *  - an EMPTIED field sends `null`, not `''` — "no unit" has to read as absent
 *    everywhere downstream, and `''` is a value the column can hold and every read
 *    surface would then have to defend against separately;
 *  - clearing is not mistaken for a conflict (the guard compares SERVER state to the
 *    baseline, never the draft to the baseline);
 *  - a concurrent change to the unit refuses the write and raises the dialog, titled
 *    for THIS editor;
 *  - an unrelated concurrent change (version bumped, unit untouched) does NOT interrupt
 *    — `WorkOrder` maps `version_id_col`, so every operation completion bumps it, and a
 *    version-keyed check would make the unit unsettable on any live job.
 *
 * Harness mirrors WorkOrderDetail.notesEdit.test.tsx, which holds the same guard for
 * the other two editors. The happy path is asserted here rather than there because this
 * editor is new; the shared dialog's own mechanics are not re-proven.
 */

import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';
import { ToastProvider } from '../components/ui';
import { useWebSocket } from '../hooks/useWebSocket';
import { WorkOrder } from '../types';

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
    getMaterialAllocations: jest.fn(),
    getWorkCenters: jest.fn(),
    updateWorkOrder: jest.fn(),
    updateOperation: jest.fn(),
    startWorkOrder: jest.fn(),
    releaseWorkOrder: jest.fn(),
    deleteWorkOrder: jest.fn(),
  },
}));

// Mutable so the role-gate test can demote the viewer (jest allows lazily referenced
// `mock*` variables inside the hoisted factory).
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

const UNIT = '2410048'; // what the server holds when the editor opens
const MY_DRAFT = '2410052'; // what this planner types
const THEIR_UNIT = '2410099'; // what a concurrent planner sets meanwhile

function makeWorkOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 42,
    version: 3,
    work_order_number: 'WO-0042',
    part_id: 100,
    work_order_type: 'production',
    quantity_ordered: 1,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 8,
    actual_hours: 0,
    unit_number: UNIT,
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

/**
 * Lands a concurrent server-side change the way production does: through the page's own
 * `onMessage` handler (captured off the mocked useWebSocket), which debounces 500ms in
 * `scheduleRealtimeRefresh` before refetching — hence the fake timers.
 *
 * `next` is the WHOLE work order the server now holds, not a delta, so a caller whose
 * guard is keyed on a field must RESTATE that field when it is meant to be unchanged.
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
    await jest.advanceTimersByTimeAsync(500);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
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

afterEach(() => {
  jest.useRealTimers();
});

/** Renders, waits for the WO, opens the unit-# pencil, returns the input. */
async function openUnitNumberEditor() {
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit unit #' }));
  return screen.getByLabelText('Unit #') as HTMLInputElement;
}

async function clickSaveUnitNumber() {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Save unit #' }));
  });
}

describe('WorkOrderDetail inline unit-# edit', () => {
  it('opens seeded with the server value and saves { unit_number, version }', async () => {
    const input = await openUnitNumberEditor();
    expect(input).toHaveValue(UNIT);

    fireEvent.change(input, { target: { value: MY_DRAFT } });
    await clickSaveUnitNumber();

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    // Exactly two keys: this editor must not carry any other field along with it,
    // because PUT /work-orders/{id} is a blind setattr loop on whatever it is sent.
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      unit_number: MY_DRAFT,
      version: 3,
    });
    expect(await screen.findByText(`Unit # set to ${MY_DRAFT}`)).toBeInTheDocument();
  });

  it('trims the typed value before sending it', async () => {
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: '  2410052 ' } });

    await clickSaveUnitNumber();

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      unit_number: MY_DRAFT,
      version: 3,
    });
  });

  it('clears the unit with null (not "") when the field is emptied', async () => {
    // A unit typed onto the wrong work order is already on the kiosk and the TV wall,
    // so it has to be REMOVABLE, not merely overwritable. `null`, not `''`: "no unit"
    // must read as absent everywhere downstream rather than as a present-but-blank
    // value each read surface has to collapse on its own.
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: '' } });

    await clickSaveUnitNumber();

    // The guard compares SERVER state to the baseline, never the draft to the baseline
    // — an emptied input is this user's own edit, not somebody else's.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      unit_number: null,
      version: 3,
    });
    expect(await screen.findByText('Unit # cleared')).toBeInTheDocument();
  });

  it('clears a whitespace-only draft to null as well', async () => {
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: '   ' } });

    await clickSaveUnitNumber();

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      unit_number: null,
      version: 3,
    });
  });

  it('refuses the save and opens the dialog when the unit changed under the editor', async () => {
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, unit_number: THEIR_UNIT });
    await clickSaveUnitNumber();

    // The whole point: no write went out.
    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    // Titled for THIS editor — the shared dialog must not tell a planner re-keying a
    // unit that their notes changed.
    expect(within(dialog).getByText('Unit # changed by someone else')).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Someone else changed the unit # on WO-0042 while you were editing\./)
    ).toBeInTheDocument();
    // No false success.
    expect(screen.queryByText(/^Unit # set to/)).not.toBeInTheDocument();
  });

  it('"Replace with mine" writes the draft at the version the concurrent write bumped it to', async () => {
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, unit_number: THEIR_UNIT });
    await clickSaveUnitNumber();
    await screen.findByRole('dialog');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Replace with mine' }));
    });

    expect(mockedApi.updateWorkOrder).toHaveBeenCalledTimes(1);
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      unit_number: MY_DRAFT,
      version: 7,
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText(`Unit # set to ${MY_DRAFT}`)).toBeInTheDocument();
  });

  it('"Keep editing" makes no API call and leaves the draft intact', async () => {
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, unit_number: THEIR_UNIT });
    await clickSaveUnitNumber();
    await screen.findByRole('dialog');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
    });

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // "Keep editing" has to mean keep editing, not discard and reopen.
    expect(screen.getByLabelText('Unit #')).toHaveValue(MY_DRAFT);
  });

  it('does not fire on an unrelated concurrent change (version bumped, unit untouched)', async () => {
    // The design decision this pins: the guard is keyed on the UNIT, so a job that is
    // simply running — operations completing, each bumping version_id_col — must stay
    // re-keyable. A version-keyed check would put this dialog in front of the planner
    // on every live work order.
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({
      version: 9,
      unit_number: UNIT, // restated: the server did NOT move the unit
      status: 'in_progress',
      quantity_complete: 1,
    });
    await clickSaveUnitNumber();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockedApi.updateWorkOrder).toHaveBeenCalledWith(42, {
      unit_number: MY_DRAFT,
      version: 9,
    });
  });

  it('fires when a unit is ADDED under an editor opened on a work order that had none', async () => {
    // The null -> value direction. `unitNumberServerValue` normalizes a missing unit to
    // '' for the comparison, so this case only works if the baseline is captured from
    // that same normalization — otherwise a first-time assignment by somebody else is
    // silently overwritten, which is the exact double-assignment 083 exists to make
    // visible.
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ unit_number: null }));

    const input = await openUnitNumberEditor();
    expect(input).toHaveValue('');
    fireEvent.change(input, { target: { value: MY_DRAFT } });

    await pushConcurrentChange({ version: 7, unit_number: THEIR_UNIT });
    await clickSaveUnitNumber();

    expect(mockedApi.updateWorkOrder).not.toHaveBeenCalled();
    expect(within(await screen.findByRole('dialog')).getByText('Unit # changed by someone else')).toBeInTheDocument();
  });

  it('surfaces the server detail verbatim and keeps the editor open on a refusal', async () => {
    const input = await openUnitNumberEditor();
    fireEvent.change(input, { target: { value: MY_DRAFT } });
    mockedApi.updateWorkOrder.mockRejectedValue({
      response: { status: 409, data: { detail: 'Work order was modified by someone else.' } },
    });

    await clickSaveUnitNumber();

    expect(await screen.findByText('Work order was modified by someone else.')).toBeInTheDocument();
    expect(screen.queryByText(/^Unit # set to/)).not.toBeInTheDocument();
    // Still open, still holding what they typed — a retry must not cost them the value.
    expect(screen.getByLabelText('Unit #')).toHaveValue(MY_DRAFT);
  });

  it('hides the pencil from a role that cannot edit the work order', async () => {
    // No new gate: the editor rides the existing PUT /work-orders/{id} tier, so a
    // hidden control and a refused request agree.
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderDetail();

    // The work order rendered (so the absence below is a gate, not a failed load).
    // `findAllBy`: the number appears in both the breadcrumb and the hero heading.
    expect(await screen.findAllByText('WO-0042')).not.toHaveLength(0);
    expect(screen.queryByRole('button', { name: 'Edit unit #' })).not.toBeInTheDocument();
    // The VALUE is still readable — a role that cannot edit the unit can still see it.
    expect(screen.getAllByTestId('unit-badge').length).toBeGreaterThan(0);
  });

  it('shows the unit badge in the hero, and renders none when the work order has no unit', async () => {
    const withUnit = renderDetail();
    expect(await screen.findAllByTestId('unit-badge')).not.toHaveLength(0);
    withUnit.unmount();

    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ unit_number: null }));
    renderDetail();
    // Wait for the SAME load to finish, then assert the badge is absent — otherwise
    // this passes vacuously against a page that simply had not rendered yet.
    expect(await screen.findAllByText('WO-0042')).not.toHaveLength(0);
    expect(screen.queryByTestId('unit-badge')).not.toBeInTheDocument();
  });
});
