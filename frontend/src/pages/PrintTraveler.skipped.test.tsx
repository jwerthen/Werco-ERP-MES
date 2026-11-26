/**
 * Traveler — a skipped step keeps who / when / why on paper (owner decision 2026-11-25).
 *
 * The row prints "SKIPPED — do not run", the reason, the person who skipped it (the
 * server-resolved name, else "User #N") and the Central time it was skipped, and takes no
 * operator sign-off block.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../services/api';
import PrintTraveler from './PrintTraveler';
import { formatCentralDateTime } from '../utils/centralTime';
import type { Part, WorkOrder, WorkOrderOperation } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getPart: jest.fn(),
    getMaterialRequirements: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 7, first_name: 'Quinn', last_name: 'Printer', role: 'supervisor' },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('qrcode', () => ({
  __esModule: true,
  default: { toDataURL: jest.fn().mockResolvedValue('data:image/png;base64,') },
}));

const mockedApi = api as jest.Mocked<typeof api>;

function op(overrides: Partial<WorkOrderOperation>): WorkOrderOperation {
  return {
    id: 1,
    version: 1,
    work_order_id: 42,
    work_center_id: 1,
    work_center_name: 'Paint Booth',
    sequence: 10,
    operation_number: '10',
    name: 'Step',
    setup_time_hours: 0,
    run_time_hours: 1,
    run_time_per_piece: 0,
    actual_setup_hours: 0,
    actual_run_hours: 0,
    status: 'pending',
    quantity_complete: 0,
    quantity_scrapped: 0,
    requires_inspection: false,
    inspection_complete: false,
    created_at: '2026-11-25T00:00:00Z',
    updated_at: '2026-11-25T00:00:00Z',
    ...overrides,
  };
}

const SKIPPED_AT = '2026-11-25T15:00:00Z';

function workOrder(operations: WorkOrderOperation[]): WorkOrder {
  return {
    id: 42,
    version: 1,
    work_order_number: 'WO-SKIP-1',
    part_id: 9,
    work_order_type: 'production',
    sequential_operations: true,
    quantity_ordered: 2,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 1,
    actual_hours: 0,
    created_at: '2026-11-25T00:00:00Z',
    updated_at: '2026-11-25T00:00:00Z',
    operations,
  };
}

const PART: Part = {
  id: 9,
  version: 0,
  part_number: 'P-9',
  name: 'Bracket',
  revision: 'A',
  unit_of_measure: 'each',
  part_type: 'manufactured',
  standard_cost: 1,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-08-01T12:00:00Z',
  updated_at: '2026-08-01T12:00:00Z',
};

function renderTraveler() {
  return render(
    <MemoryRouter initialEntries={['/print/traveler/42']}>
      <Routes>
        <Route path="/print/traveler/:id" element={<PrintTraveler />} />
      </Routes>
    </MemoryRouter>
  );
}

function rowFor(name: string): HTMLElement {
  const row = screen.getByText(name).closest('tr');
  if (!row) throw new Error(`no routing row for ${name}`);
  return row as HTMLElement;
}

describe('PrintTraveler — skipped step record', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getPart.mockResolvedValue(PART);
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-SKIP-1',
      quantity_ordered: 2,
      has_bom: false,
      materials: [],
    });
  });

  it('prints who skipped the step, when (Central) and why, with no sign-off block', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      workOrder([
        op({
          id: 1,
          name: 'Paint',
          status: 'skipped',
          skip_reason: 'Customer supplies finish',
          skipped_by: 5,
          skipped_by_name: 'Sam Supervisor',
          skipped_at: SKIPPED_AT,
        }),
      ])
    );
    renderTraveler();
    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());

    const row = rowFor('Paint');
    expect(within(row).getByText('SKIPPED — do not run')).toBeInTheDocument();
    expect(within(row).getByText('Customer supplies finish')).toBeInTheDocument();
    expect(within(row).getByText(`Sam Supervisor · ${formatCentralDateTime(SKIPPED_AT)}`)).toBeInTheDocument();
    expect(within(row).queryByText(/Operator:/)).not.toBeInTheDocument();
  });

  it('falls back to "User #N" when the name could not be resolved', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(
      workOrder([
        op({
          id: 1,
          name: 'Paint',
          status: 'skipped',
          skip_reason: 'Not needed',
          skipped_by: 5,
          skipped_by_name: null,
          skipped_at: SKIPPED_AT,
        }),
      ])
    );
    renderTraveler();
    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());

    expect(within(rowFor('Paint')).getByText(`User #5 · ${formatCentralDateTime(SKIPPED_AT)}`)).toBeInTheDocument();
  });
});
