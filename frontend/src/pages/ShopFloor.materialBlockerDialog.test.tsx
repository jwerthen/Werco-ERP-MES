/**
 * ShopFloor — the "No Material" blocker report now captures its note through
 * the shared InputDialog instead of the native window.prompt().
 *
 * Covers: the queue-row button opens the dialog pre-filled with the standard
 * operator note, submit posts the blocker (entered note, material_missing /
 * high / hold) and refreshes the queue, cancel posts nothing, a refusal keeps
 * the dialog open with the verbatim error and a working retry, the re-entry
 * guard makes rapid Enter+click submit exactly once, and consecutive opens
 * across rows never leak the previous row's context. Harness mirrors
 * ShopFloor.runOrder.test.tsx.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloor from './ShopFloor';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkOrder: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    updateWorkOrderPriority: jest.fn(),
    createWorkOrderBlocker: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'operator', is_superuser: false },
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

const DEFAULT_NOTE = 'Operator reported material is not available at the work center.';

const workCenter = {
  id: 7,
  version: 1,
  code: 'CNC-1',
  name: 'CNC Mill 1',
  work_center_type: 'milling',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const queueItem = {
  operation_id: 101,
  work_order_id: 1001,
  work_order_number: 'WO-9001',
  part_number: 'PN-XXX',
  part_name: 'Fixture Plate',
  operation_number: '10',
  operation_name: 'Mill',
  status: 'ready',
  quantity_ordered: 10,
  quantity_complete: 0,
  setup_time_hours: 1,
  run_time_hours: 4,
  run_order: 1,
  priority: 5,
  due_date: '2099-01-05',
};

function renderShopFloor() {
  return render(
    <MemoryRouter>
      <ShopFloor />
    </MemoryRouter>
  );
}

async function openBlockerDialog() {
  renderShopFloor();
  fireEvent.click(await screen.findByRole('button', { name: /no material/i }));
  return await screen.findByRole('dialog');
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue([workCenter]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem] });
  mockedApi.createWorkOrderBlocker.mockResolvedValue({ id: 55 });
});

describe('ShopFloor material-blocker InputDialog', () => {
  it('opens the dialog pre-filled with the standard note and names the work order', async () => {
    await openBlockerDialog();

    expect(screen.getByText('Report Missing Material')).toBeInTheDocument();
    expect(screen.getByText(/Report missing material for WO-9001\?/)).toBeInTheDocument();
    expect(screen.getByLabelText(/note/i)).toHaveValue(DEFAULT_NOTE);
    // Nothing posted just by opening.
    expect(mockedApi.createWorkOrderBlocker).not.toHaveBeenCalled();
  });

  it('submit posts the blocker with the entered note, refreshes the queue, and closes', async () => {
    await openBlockerDialog();

    fireEvent.change(screen.getByLabelText(/note/i), { target: { value: '  4140 plate not at the saw  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Report' }));

    await waitFor(() =>
      expect(mockedApi.createWorkOrderBlocker).toHaveBeenCalledWith(1001, {
        operation_id: 101,
        category: 'material_missing',
        severity: 'high',
        note: '4140 plate not at the saw',
        put_operation_on_hold: true,
      })
    );

    // Non-optimistic: the queue is re-fetched (initial load + post-report refresh).
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancel closes the dialog and posts nothing', async () => {
    await openBlockerDialog();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.createWorkOrderBlocker).not.toHaveBeenCalled();
    expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledTimes(1);
  });

  it('a refused report keeps the dialog open with the verbatim error and clears the guard so retry works', async () => {
    const refusal = 'Operation already has an open material blocker';
    mockedApi.createWorkOrderBlocker.mockRejectedValueOnce({
      response: { data: { detail: refusal } },
    });
    await openBlockerDialog();

    fireEvent.click(screen.getByRole('button', { name: 'Report' }));

    // Verbatim server detail surfaces; the dialog STAYS OPEN (InputDialog
    // callers keep the typed note up for retry — unlike ConfirmDialogs, which
    // close on settle either way).
    expect(await screen.findByText(refusal)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByLabelText(/note/i)).toHaveValue(DEFAULT_NOTE);

    // The in-flight guard is cleared after the rejection settles: the submit
    // re-enables and a retry posts again (second call), then closes.
    const report = screen.getByRole('button', { name: 'Report' });
    await waitFor(() => expect(report).toBeEnabled());
    fireEvent.click(report);

    await waitFor(() => expect(mockedApi.createWorkOrderBlocker).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('rapid Enter + click submits exactly once while the request hangs (re-entry guard)', async () => {
    let resolveCreate!: (value: unknown) => void;
    mockedApi.createWorkOrderBlocker.mockImplementation(
      () => new Promise((resolve) => { resolveCreate = resolve; })
    );
    const user = userEvent.setup();
    const dialog = await openBlockerDialog();

    const input = screen.getByLabelText(/note/i);
    // Enter submits...
    await user.type(input, '{Enter}');
    await waitFor(() => expect(mockedApi.createWorkOrderBlocker).toHaveBeenCalledTimes(1));

    // ...and while the request hangs, neither a click on the (now disabled)
    // submit button nor a direct form re-submit fires a second call. Scoped to
    // the dialog (the queue row's own button reads "Reporting..." mid-flight);
    // regex because the spinner's aria-label makes the name "Loading Report".
    fireEvent.click(within(dialog).getByRole('button', { name: /report/i }));
    fireEvent.submit(input.closest('form')!);
    expect(mockedApi.createWorkOrderBlocker).toHaveBeenCalledTimes(1);

    // Settle to close cleanly.
    resolveCreate({ id: 55 });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.createWorkOrderBlocker).toHaveBeenCalledTimes(1);
  });

  it("consecutive opens carry each row's own context — no stale text from the previous dialog", async () => {
    const queueItemB = {
      ...queueItem,
      operation_id: 102,
      work_order_id: 1002,
      work_order_number: 'WO-9002',
      run_order: 2,
    };
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [queueItem, queueItemB] });
    renderShopFloor();

    // Open row A's dialog and dirty the note.
    const buttons = await screen.findAllByRole('button', { name: /no material/i });
    fireEvent.click(buttons[0]);
    await screen.findByRole('dialog');
    expect(screen.getByText(/Report missing material for WO-9001\?/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/note/i), { target: { value: 'Row A custom note' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // Open row B's dialog: B's message, and the note re-seeded to the default.
    fireEvent.click(screen.getAllByRole('button', { name: /no material/i })[1]);
    const dialogB = await screen.findByRole('dialog');
    expect(screen.getByText(/Report missing material for WO-9002\?/)).toBeInTheDocument();
    expect(dialogB.textContent).not.toContain('WO-9001');
    expect(screen.getByLabelText(/note/i)).toHaveValue(DEFAULT_NOTE);
    expect(mockedApi.createWorkOrderBlocker).not.toHaveBeenCalled();
  });
});
