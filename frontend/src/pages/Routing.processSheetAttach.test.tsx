/**
 * Routing — process-sheet attach control (PR 2 of docs/PROCESS_SHEETS_SCOPE.md).
 *
 * The operation modal on a DRAFT routing carries a "Process Sheet" select
 * listing RELEASED sheets. Guards:
 *   - released sheets appear as options and selecting one sends its id;
 *   - the explicit "None" option sends process_sheet_id: null (detach);
 *   - an attached sheet shows a row-targeted link to /process-sheets?sheet=<id>;
 *   - the released-routing time-standards payload still excludes the field.
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

const releasedSheetListItem = {
  id: 55,
  sheet_number: 'PS-000055',
  title: 'Weld Seam Inspection',
  revision: 'A',
  status: 'released',
  is_active: true,
  effective_date: '2026-06-01T12:00:00Z',
  step_count: 3,
  created_at: '2026-05-01T12:00:00Z',
  updated_at: '2026-06-01T12:00:00Z',
};

const baseOperation = {
  id: 200,
  routing_id: 2,
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
  process_sheet_id: null as number | null,
};

const draftRouting = {
  id: 2,
  part_id: 11,
  part: { id: 11, part_number: 'PN-DRAFT', name: 'Draft Bracket', part_type: 'manufactured' },
  revision: 'A',
  status: 'draft',
  is_active: true,
  total_setup_hours: 0.5,
  total_run_hours_per_unit: 0.1,
  total_labor_cost: 57,
  operations: [baseOperation],
  created_at: '2026-01-01T00:00:00Z',
};

const draftRoutingWithAttachedSheet = {
  ...draftRouting,
  operations: [{ ...baseOperation, process_sheet_id: 55 }],
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

async function openEditOperationModal(routing: typeof draftRouting) {
  mockedApi.getRouting.mockResolvedValue(routing);
  renderPage();
  fireEvent.click(await screen.findByText(routing.part!.part_number));
  await screen.findByText(routing.operations[0].name);
  fireEvent.click(screen.getByTitle('Edit operation'));
  return screen.findByRole('dialog');
}

describe('Routing — process-sheet attach control', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUser = { id: 1, role: 'admin', is_superuser: false };
    mockedApi.getRoutings.mockResolvedValue([draftRouting]);
    mockedApi.getParts.mockResolvedValue([]);
    mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
    mockedApi.getProcessSheets.mockResolvedValue([releasedSheetListItem]);
    mockedApi.updateRoutingOperation.mockResolvedValue({});
    mockedApi.addRoutingOperation.mockResolvedValue({});
    jest.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('loads released sheets and offers them in the Process Sheet select', async () => {
    const dialog = await openEditOperationModal(draftRouting);

    await waitFor(() => {
      expect(mockedApi.getProcessSheets).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'released' })
      );
    });

    fireEvent.click(within(dialog).getByLabelText('Process sheet'));
    const listbox = await screen.findByRole('listbox');
    expect(within(listbox).getByText('None')).toBeInTheDocument();
    expect(within(listbox).getByText('PS-000055 Rev A')).toBeInTheDocument();
    expect(within(listbox).getByText('Weld Seam Inspection')).toBeInTheDocument();
  });

  it('selecting a released sheet sends its id in the operation payload', async () => {
    const dialog = await openEditOperationModal(draftRouting);

    fireEvent.click(within(dialog).getByLabelText('Process sheet'));
    const listbox = await screen.findByRole('listbox');
    fireEvent.mouseDown(within(listbox).getByRole('option', { name: /PS-000055 Rev A/ }));

    fireEvent.click(within(dialog).getByText('Update Operation'));

    await waitFor(() => expect(mockedApi.updateRoutingOperation).toHaveBeenCalledTimes(1));
    const [routingId, opId, payload] = mockedApi.updateRoutingOperation.mock.calls[0];
    expect(routingId).toBe(2);
    expect(opId).toBe(200);
    expect(payload.process_sheet_id).toBe(55);
  });

  it('shows a row-targeted link for an attached sheet and detaches with an explicit null', async () => {
    mockedApi.getRoutings.mockResolvedValue([draftRoutingWithAttachedSheet]);
    const dialog = await openEditOperationModal(draftRoutingWithAttachedSheet);

    // The attached sheet is displayed and linked out to the ProcessSheets page,
    // row-targeted via the ?sheet= URL param.
    const link = within(dialog).getByRole('link', { name: /View process sheet/ });
    expect(link).toHaveAttribute('href', '/process-sheets?sheet=55');

    // Explicit "None" -> process_sheet_id: null (the server treats null as detach).
    fireEvent.click(within(dialog).getByLabelText('Process sheet'));
    const listbox = await screen.findByRole('listbox');
    fireEvent.mouseDown(within(listbox).getByRole('option', { name: /None/ }));

    fireEvent.click(within(dialog).getByText('Update Operation'));

    await waitFor(() => expect(mockedApi.updateRoutingOperation).toHaveBeenCalledTimes(1));
    const [, , payload] = mockedApi.updateRoutingOperation.mock.calls[0];
    expect(payload.process_sheet_id).toBeNull();
  });

  it('adding a new operation without a sheet sends process_sheet_id: null', async () => {
    mockedApi.getRouting.mockResolvedValue(draftRouting);
    renderPage();
    fireEvent.click(await screen.findByText('PN-DRAFT'));
    await screen.findByText('Laser Cut');
    fireEvent.click(screen.getByText('Add Operation'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Operation Name/), { target: { value: 'Deburr' } });
    fireEvent.change(within(dialog).getByLabelText(/Work Center/), { target: { value: '5' } });
    fireEvent.click(within(dialog).getByText('Add Operation', { selector: 'button[type="submit"]' }));

    await waitFor(() => expect(mockedApi.addRoutingOperation).toHaveBeenCalledTimes(1));
    const [, payload] = mockedApi.addRoutingOperation.mock.calls[0];
    expect(payload.process_sheet_id).toBeNull();
  });

  it('keeps the process-sheet field out of the released time-standards payload', async () => {
    const releasedRouting = {
      ...draftRoutingWithAttachedSheet,
      id: 3,
      status: 'released',
      part: { id: 12, part_number: 'PN-REL', name: 'Released Bracket', part_type: 'manufactured' },
    };
    mockedApi.getRoutings.mockResolvedValue([releasedRouting]);
    mockedApi.getRouting.mockResolvedValue(releasedRouting);
    renderPage();
    fireEvent.click(await screen.findByText('PN-REL'));
    await screen.findByText('Laser Cut');
    fireEvent.click(screen.getByTitle('Edit time standards'));

    const dialog = await screen.findByRole('dialog');
    // The structural attach control is not offered on a released routing.
    expect(within(dialog).queryByLabelText('Process sheet')).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByText('Save Time Standards'));
    await waitFor(() => expect(mockedApi.updateRoutingOperation).toHaveBeenCalledTimes(1));
    const [, , payload] = mockedApi.updateRoutingOperation.mock.calls[0];
    expect(payload).not.toHaveProperty('process_sheet_id');
  });
});
