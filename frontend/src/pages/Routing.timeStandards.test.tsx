/**
 * Routing — released time-standard editing.
 *
 * A RELEASED routing's process is locked, but its TIME STANDARDS (setup, run,
 * move, queue, cycle time, pieces/cycle) can be adjusted as actuals come in —
 * gated to Admin/Manager/platform_admin (mirrors the backend's released-edit
 * path, which 403s a Supervisor). This guards:
 *   - the per-row "Edit times" action shows for Admin/Manager on a released
 *     routing and is hidden for Supervisor/Operator;
 *   - the released edit modal renders ONLY the time fields (no Work Center /
 *     Operation Name / Sequence / inspection flags);
 *   - saving sends only the time-standard fields to updateRoutingOperation;
 *   - a 403 and a 400 from the server are surfaced to the user;
 *   - a DRAFT routing still shows the full Edit + Delete controls and modal.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import RoutingPage from './Routing';
import { ToastProvider } from '../components/ui';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getRoutings: jest.fn(),
    getParts: jest.fn(),
    getWorkCenters: jest.fn(),
    getRouting: jest.fn(),
    getPart: jest.fn(),
    getProcessSheets: jest.fn(),
    getProcessSheet: jest.fn(),
    updateRoutingOperation: jest.fn(),
    addRoutingOperation: jest.fn(),
    deleteRoutingOperation: jest.fn(),
    releaseRouting: jest.fn(),
    deleteRouting: jest.fn(),
    createRouting: jest.fn(),
  },
}));

// Mutable mock user so each test can pick a role before rendering.
let mockUser: { id: number; role: string; is_superuser?: boolean } = {
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

const mockedApi = api as jest.Mocked<typeof api>;

const workCenter = { id: 5, code: 'WC-LASER', name: 'Laser Cell', work_center_type: 'fabrication', hourly_rate: 95 };

const releasedOperation = {
  id: 100,
  routing_id: 1,
  sequence: 10,
  operation_number: '010',
  name: 'Laser Cut',
  description: 'Cut blank to size',
  work_center_id: 5,
  work_center: workCenter,
  setup_hours: 0.5,
  run_hours_per_unit: 0.1,
  move_hours: 0,
  queue_hours: 0,
  cycle_time_seconds: 30,
  pieces_per_cycle: 1,
  is_inspection_point: false,
  is_outside_operation: false,
  is_active: true,
};

const releasedRouting = {
  id: 1,
  part_id: 10,
  part: { id: 10, part_number: 'PN-REL', name: 'Released Bracket', part_type: 'manufactured' },
  revision: 'A',
  status: 'released',
  is_active: true,
  total_setup_hours: 0.5,
  total_run_hours_per_unit: 0.1,
  total_labor_cost: 57,
  operations: [releasedOperation],
  created_at: '2026-01-01T00:00:00Z',
};

const draftRouting = {
  ...releasedRouting,
  id: 2,
  status: 'draft',
  part: { id: 11, part_number: 'PN-DRAFT', name: 'Draft Bracket', part_type: 'manufactured' },
  operations: [{ ...releasedOperation, id: 200, routing_id: 2 }],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <RoutingPage />
      </ToastProvider>
    </MemoryRouter>
  );
}

/** Render, then click a routing in the list to load it as the selected routing. */
async function selectRouting(routing: typeof releasedRouting) {
  mockedApi.getRouting.mockResolvedValue(routing);
  renderPage();
  const listEntry = await screen.findByText(routing.part!.part_number);
  fireEvent.click(listEntry);
  // The detail header repeats the part number — wait for the operation row.
  await screen.findByText(routing.operations[0].name);
}

describe('Routing — released time-standard editing', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUser = { id: 1, role: 'admin', is_superuser: false };
    mockedApi.getRoutings.mockResolvedValue([releasedRouting, draftRouting]);
    mockedApi.getParts.mockResolvedValue([]);
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getProcessSheets.mockResolvedValue([]);
    mockedApi.updateRoutingOperation.mockResolvedValue({});
    jest.spyOn(window, 'alert').mockImplementation(() => {});
    jest.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the Edit times action on a released routing for an Admin', async () => {
    mockUser = { id: 1, role: 'admin' };
    await selectRouting(releasedRouting);
    expect(screen.getByTitle('Edit time standards')).toBeInTheDocument();
    // Structural deletion stays blocked on released routings.
    expect(screen.queryByTitle('Delete operation')).not.toBeInTheDocument();
  });

  it('shows the Edit times action on a released routing for a Manager', async () => {
    mockUser = { id: 2, role: 'manager' };
    await selectRouting(releasedRouting);
    expect(screen.getByTitle('Edit time standards')).toBeInTheDocument();
  });

  it('hides the Edit times action on a released routing for a Supervisor', async () => {
    mockUser = { id: 3, role: 'supervisor' };
    await selectRouting(releasedRouting);
    expect(screen.queryByTitle('Edit time standards')).not.toBeInTheDocument();
  });

  it('hides the Edit times action on a released routing for an Operator', async () => {
    mockUser = { id: 4, role: 'operator' };
    await selectRouting(releasedRouting);
    expect(screen.queryByTitle('Edit time standards')).not.toBeInTheDocument();
  });

  it('released edit modal renders only time fields — no Work Center / Operation Name / Sequence', async () => {
    mockUser = { id: 1, role: 'admin' };
    await selectRouting(releasedRouting);
    fireEvent.click(screen.getByTitle('Edit time standards'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Edit Time Standards')).toBeInTheDocument();
    // Time-standard inputs are present.
    expect(within(dialog).getByText('Setup Time')).toBeInTheDocument();
    expect(within(dialog).getByText('Run Time/Unit')).toBeInTheDocument();
    expect(within(dialog).getByText('Move Time')).toBeInTheDocument();
    expect(within(dialog).getByText('Queue Time')).toBeInTheDocument();
    expect(within(dialog).getByText('Cycle Time')).toBeInTheDocument();
    expect(within(dialog).getByText('Pieces / Cycle')).toBeInTheDocument();
    // No EDITABLE structural / process controls remain. These labels/inputs are
    // unique to the full (draft) modal. (The locked op + work center may appear
    // as read-only context, which is fine — they aren't form fields.)
    expect(within(dialog).queryByText('Sequence #')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Operation Name')).not.toBeInTheDocument();
    expect(within(dialog).queryByPlaceholderText(/Cut to size/)).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Inspection Point')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Outside Operation')).not.toBeInTheDocument();
    // The only <select> elements left are the min/hrs unit toggles — no Work
    // Center selector with its work-center options.
    expect(within(dialog).queryByRole('option', { name: /WC-LASER/ })).not.toBeInTheDocument();
  });

  it('saving a released edit sends only the time-standard fields and refreshes', async () => {
    mockUser = { id: 1, role: 'admin' };
    await selectRouting(releasedRouting);
    fireEvent.click(screen.getByTitle('Edit time standards'));
    const dialog = await screen.findByRole('dialog');

    fireEvent.click(within(dialog).getByText('Save Time Standards'));

    await waitFor(() => {
      expect(mockedApi.updateRoutingOperation).toHaveBeenCalledTimes(1);
    });
    const [routingId, opId, payload] = mockedApi.updateRoutingOperation.mock.calls[0];
    expect(routingId).toBe(1);
    expect(opId).toBe(100);
    // Only time-standard keys are sent — no structural fields.
    expect(Object.keys(payload).sort()).toEqual(
      ['cycle_time_seconds', 'move_hours', 'pieces_per_cycle', 'queue_hours', 'run_hours_per_unit', 'setup_hours'].sort()
    );
    expect(payload).not.toHaveProperty('work_center_id');
    expect(payload).not.toHaveProperty('name');
    expect(payload).not.toHaveProperty('sequence');
    // Routing reloaded after a successful save (initial load + reload).
    expect(mockedApi.getRouting).toHaveBeenCalled();
  });

  it('surfaces a 403 from the server with a role-specific message', async () => {
    mockUser = { id: 1, role: 'admin' };
    mockedApi.updateRoutingOperation.mockRejectedValue({ response: { status: 403, data: { detail: 'nope' } } });
    await selectRouting(releasedRouting);
    fireEvent.click(screen.getByTitle('Edit time standards'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByText('Save Time Standards'));

    await waitFor(() => {
      expect(
        screen.getByText("You need the Admin or Manager role to edit a released routing's time standards.")
      ).toBeInTheDocument();
    });
  });

  it('surfaces the server message on a 400', async () => {
    mockUser = { id: 1, role: 'admin' };
    const serverMsg =
      'Released routing: only time standards (setup, run/unit, move, queue, cycle) can be edited — create a new revision to change the process.';
    mockedApi.updateRoutingOperation.mockRejectedValue({ response: { status: 400, data: { detail: serverMsg } } });
    await selectRouting(releasedRouting);
    fireEvent.click(screen.getByTitle('Edit time standards'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByText('Save Time Standards'));

    await waitFor(() => {
      expect(screen.getByText(serverMsg)).toBeInTheDocument();
    });
  });

  it('draft routing still shows full Edit + Delete controls and the full modal', async () => {
    mockUser = { id: 3, role: 'supervisor' };
    await selectRouting(draftRouting);

    // Full per-row controls present on a draft.
    expect(screen.getByTitle('Edit operation')).toBeInTheDocument();
    expect(screen.getByTitle('Delete operation')).toBeInTheDocument();
    expect(screen.queryByTitle('Edit time standards')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Edit operation'));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Edit Operation')).toBeInTheDocument();
    // Full modal retains the structural fields.
    expect(within(dialog).getByText('Work Center')).toBeInTheDocument();
    expect(within(dialog).getByText('Operation Name')).toBeInTheDocument();
    expect(within(dialog).getByText('Sequence #')).toBeInTheDocument();
  });
});
