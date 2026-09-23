import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import { WorkOrder, WorkOrderOperation } from '../types';
import WorkOrderDetail from './WorkOrderDetail';

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
    getAIRecommendations: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({
  getAccessToken: () => 'test-token',
  buildWsUrl: () => 'ws://localhost/ws/test',
}));

const mockedApi = api as jest.Mocked<typeof api>;

function operation(overrides: Partial<WorkOrderOperation>): WorkOrderOperation {
  return {
    id: 601,
    version: 1,
    work_order_id: 106,
    work_center_id: 9,
    sequence: 10,
    operation_number: '10',
    name: 'Press brake',
    operation_group: 'BEND',
    component_part_id: 100,
    component_quantity: 16,
    setup_time_hours: 0,
    run_time_hours: 1,
    run_time_per_piece: 0,
    actual_setup_hours: 0,
    actual_run_hours: 0,
    status: 'ready',
    quantity_complete: 0,
    quantity_scrapped: 0,
    requires_inspection: false,
    inspection_complete: false,
    created_at: '2026-09-23T12:00:00Z',
    updated_at: '2026-09-23T12:00:00Z',
    ...overrides,
  };
}

function workOrder(operations: WorkOrderOperation[]): WorkOrder {
  return {
    id: 106,
    version: 1,
    work_order_number: 'WO-PRESS-BRAKE',
    part_id: 100,
    work_order_type: 'production',
    quantity_ordered: 1,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'in_progress',
    priority: 3,
    estimated_hours: 3,
    actual_hours: 1,
    created_at: '2026-09-23T12:00:00Z',
    updated_at: '2026-09-23T12:00:00Z',
    operations,
  };
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/work-orders/106']}>
      <Routes>
        <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

function progressTile() {
  const tile = screen.getByText('Op Progress').closest('div.card');
  if (!tile) throw new Error('Operation progress tile not found');
  return within(tile as HTMLElement);
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getDocuments.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getAIRecommendations.mockResolvedValue([]);
});

describe('WorkOrderDetail operation identity', () => {
  it('shows progressive operation numbers instead of a shared dependency sequence', async () => {
    const sourceOperations = [
      operation({ id: 601, operation_number: '10', name: 'Inlets out' }),
      operation({ id: 603, operation_number: 'OP30', name: 'Side panels' }),
      operation({ id: 602, operation_number: 'Op 20', name: 'Inlets in' }),
    ];
    mockedApi.getWorkOrder.mockResolvedValue(workOrder(sourceOperations));
    const { container } = renderDetail();

    await screen.findByRole('columnheader', { name: 'Op #' });
    expect(Array.from(container.querySelectorAll('tr[id^="operation-"] td:first-child'), cell => cell.textContent))
      .toEqual(['10', '20', '30']);
    expect(sourceOperations.map(op => op.id)).toEqual([601, 603, 602]);
    expect(screen.getByRole('option', { name: 'Op 20 - Inlets in' })).toHaveValue('602');
    expect(screen.getByRole('option', { name: 'Op 30 - Side panels' })).toHaveValue('603');
  });

  it.each([
    ['different labels', ['Inlets out', 'Inlets in', 'Side panels']],
    ['identical labels', ['Press brake', 'Press brake', 'Press brake']],
  ])('counts distinct operations sharing a sequence and %s independently', async (_description, names) => {
    const operations = [
      operation({ id: 601, name: names[0], status: 'complete', quantity_complete: 16 }),
      operation({ id: 602, name: names[1], status: 'in_progress', component_quantity: 12, quantity_complete: 6 }),
      operation({ id: 603, name: names[2], component_quantity: 48 }),
    ];
    mockedApi.getWorkOrder.mockResolvedValue(workOrder(operations));
    const { container } = renderDetail();

    await screen.findByRole('heading', { name: 'Operations / Routing' });
    expect(progressTile().getByText('1/3 ops')).toBeInTheDocument();
    expect(progressTile().getByText('50%')).toBeInTheDocument();
    expect(container.querySelectorAll('tr[id^="operation-"]')).toHaveLength(3);
    operations.forEach((op) => {
      const row = container.querySelector(`#operation-${op.id}`) as HTMLElement;
      expect(within(row).getByText(op.status.replace('_', ' '))).toBeInTheDocument();
    });
  });

  it('hydrates completion by operation ID without merging identical operation labels', async () => {
    const operations = [operation({ id: 601 }), operation({ id: 602 })];
    mockedApi.getWorkOrder.mockResolvedValue(workOrder(operations));
    mockedApi.getOperationDetails.mockResolvedValue({
      all_operations: [
        { ...operations[1], status: 'complete', quantity_complete: 16 },
        operations[0],
      ],
    });
    const { container } = renderDetail();

    await waitFor(() => expect(screen.getByText('1/2 ops')).toBeInTheDocument());
    expect(progressTile().getByText('50%')).toBeInTheDocument();
    expect(within(container.querySelector('#operation-601') as HTMLElement).getByText('ready')).toBeInTheDocument();
    expect(within(container.querySelector('#operation-602') as HTMLElement).getByText('complete')).toBeInTheDocument();
  });
});
