/**
 * WorkOrderDetail — changing a released work order's routing (owner decision 2026-11-25).
 *
 * The page offers "Edit routing" only where the server would accept a change: the routes'
 * role trio (ADMIN / MANAGER / SUPERVISOR) on a work order that is not terminal, not a laser
 * nest pool. A SKIPPED step stays on the traveler with who / when / why, and is FINISHED for
 * the client mirror of the predecessor gate exactly as for the server's, so the steps after it
 * stay completable. The editor's own verbs are covered in RoutingEditor.test.tsx.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
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
    // CommentsPanel (mounted on the page) loads its own thread and watch state.
    listComments: jest.fn().mockResolvedValue([]),
    getWatchState: jest.fn().mockResolvedValue({ watching: false, watcher_count: 0 }),
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

function makeOperation(overrides: Partial<WorkOrderOperation> = {}): WorkOrderOperation {
  return {
    id: 700,
    version: 1,
    work_order_id: 42,
    work_center_id: 9,
    sequence: 10,
    operation_number: '10',
    name: 'Saw',
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
    created_at: '2026-11-25T00:00:00Z',
    updated_at: '2026-11-25T00:00:00Z',
    ...overrides,
  };
}

/** 10 complete, 20 SKIPPED (who / when / why), 30 READY — the server promoted 30 past the skip. */
const OPERATIONS_WITH_SKIP: WorkOrderOperation[] = [
  makeOperation({
    id: 701,
    sequence: 10,
    operation_number: '10',
    name: 'Saw',
    status: 'complete',
    quantity_complete: 2,
  }),
  makeOperation({
    id: 702,
    sequence: 20,
    operation_number: '20',
    name: 'Paint',
    status: 'skipped',
    skip_reason: 'Customer supplies finish',
    skipped_by: 1,
    skipped_at: '2026-11-25T15:30:00Z',
  }),
  makeOperation({ id: 703, sequence: 30, operation_number: '30', name: 'Pack', status: 'ready' }),
];

function makeWorkOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 42,
    version: 3,
    work_order_number: 'WO-20261125-001',
    part_id: 100,
    work_order_type: 'production',
    sequential_operations: true,
    quantity_ordered: 2,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'in_progress',
    priority: 3,
    estimated_hours: 20,
    actual_hours: 0,
    created_at: '2026-11-25T00:00:00Z',
    updated_at: '2026-11-25T00:00:00Z',
    operations: OPERATIONS_WITH_SKIP,
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

function routingCard(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Operations / Routing' });
  const card = heading.closest('div.card');
  if (!card) throw new Error('Operations / Routing card not found');
  return card as HTMLElement;
}

function operationRow(name: string): HTMLElement {
  const row = within(routingCard()).getAllByText(name)[0].closest('tr');
  if (!row) throw new Error(`No row for ${name}`);
  return row as HTMLElement;
}

async function loaded() {
  await screen.findByRole('heading', { name: 'Operations / Routing' });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, role: 'supervisor', is_superuser: false };
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
});

describe('Edit routing — who and where', () => {
  it.each(['admin', 'manager', 'supervisor'])('is offered to %s on a released work order', async role => {
    mockUser = { id: 1, role, is_superuser: false };
    renderDetail();
    await loaded();
    expect(within(routingCard()).getByRole('button', { name: /Edit routing/ })).toBeInTheDocument();
  });

  it.each(['operator', 'quality', 'viewer'])('is not offered to %s', async role => {
    mockUser = { id: 1, role, is_superuser: false };
    renderDetail();
    await loaded();
    expect(within(routingCard()).queryByRole('button', { name: /Edit routing/ })).not.toBeInTheDocument();
  });

  it.each(['complete', 'closed', 'cancelled'] as const)('is hidden on a %s work order', async status => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ status }));
    renderDetail();
    await loaded();
    expect(within(routingCard()).queryByRole('button', { name: /Edit routing/ })).not.toBeInTheDocument();
  });

  it('is hidden on a laser nest work order (nests are managed through their own verbs)', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(makeWorkOrder({ work_order_type: 'laser_cutting' }));
    renderDetail();
    await loaded();
    expect(within(routingCard()).queryByRole('button', { name: /Edit routing/ })).not.toBeInTheDocument();
  });

  it('opens the editor in place', async () => {
    const user = userEvent.setup();
    renderDetail();
    await loaded();
    await user.click(within(routingCard()).getByRole('button', { name: /Edit routing/ }));
    expect(screen.getByRole('region', { name: 'Edit routing' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restore Op 20' })).toBeInTheDocument();
  });
});

describe('a skipped step on the traveler', () => {
  it('shows Skipped with the reason, who and when (Central)', async () => {
    renderDetail();
    await loaded();
    const disclosure = await screen.findByTestId('op-skipped-702');
    expect(disclosure).toHaveTextContent('Skipped');
    expect(disclosure).toHaveTextContent('Customer supplies finish');
    expect(disclosure).toHaveTextContent('You');
    expect(disclosure).toHaveTextContent(/Nov 25, 2026/);
    expect(disclosure).toHaveTextContent(/9:30/);
  });

  it('offers no Complete or Material on the skipped row', async () => {
    renderDetail();
    await loaded();
    const row = operationRow('Paint');
    expect(within(row).queryByRole('button', { name: 'Complete' })).not.toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: /Material/ })).not.toBeInTheDocument();
  });

  it('does not block Complete on the step after it under sequencing (the server treats it as finished)', async () => {
    renderDetail();
    await loaded();
    const complete = within(operationRow('Pack')).getByRole('button', { name: 'Complete' });
    expect(complete).toBeEnabled();
    expect(complete).toHaveAttribute('title', 'Complete Operation');
  });

  it('still blocks behind a predecessor that is genuinely unfinished', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      makeWorkOrder({
        operations: [
          makeOperation({ id: 711, sequence: 10, operation_number: '10', name: 'Saw', status: 'in_progress' }),
          makeOperation({ id: 712, sequence: 20, operation_number: '20', name: 'Paint', status: 'skipped' }),
          makeOperation({ id: 713, sequence: 30, operation_number: '30', name: 'Pack', status: 'pending' }),
        ],
      })
    );
    renderDetail();
    await loaded();
    await waitFor(() =>
      expect(within(operationRow('Pack')).getByRole('button', { name: 'Complete' })).toHaveAttribute(
        'title',
        expect.stringContaining('operation 10 (Saw)')
      )
    );
  });
});
