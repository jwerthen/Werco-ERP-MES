/**
 * Traveler "Component / Qty Req" — the number an operator builds to.
 *
 * The bug this pins (2026-08-13): the cell branched on `component_part_number` and
 * fell back to `workOrder.quantity_ordered`, so on a BATCH / POOL work order — one
 * operation per fabricated line item, each carrying its own `component_quantity` with
 * NO component part number — every line of the printed traveler told the operator to
 * make the work order's set quantity instead of that item's piece count. Paper an
 * operator works from, not a screen.
 *
 * The cell now uses the same rule as everything else (`operationTargetQuantity`, the
 * client mirror of the server's `operation_target_quantity`): the operation's own
 * target when it has one, the work-order quantity otherwise. The component identity
 * lines still print for BOM component operations.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import QRCode from 'qrcode';
import api from '../services/api';
import PrintTraveler from './PrintTraveler';
import type { Part } from '../types';

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
  default: { toDataURL: jest.fn() },
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedToDataURL = QRCode.toDataURL as jest.Mock;

/** A batch/pool WO: 8 sets ordered, three line items with their own piece counts. */
const POOL_WORK_ORDER = {
  id: 42,
  work_order_number: 'WO-20260812-003',
  part_id: 9,
  status: 'in_progress',
  priority: 3,
  quantity_ordered: 8,
  quantity_complete: 8,
  customer_name: 'Acme Aero',
  customer_po: 'PO-1',
  due_date: '2026-08-20',
  operations: [
    {
      id: 101,
      sequence: 10,
      operation_number: 'OP10',
      name: 'ITEM 62: DOOR SKIN',
      work_center_id: 1,
      work_center_name: 'Ermaksan EVO-III',
      status: 'complete',
      setup_time_hours: 0.5,
      run_time_hours: 2,
      component_quantity: 8,
    },
    {
      id: 102,
      sequence: 20,
      operation_number: 'OP20',
      name: 'ITEM 63: DOOR FRAME LH',
      work_center_id: 1,
      work_center_name: 'Ermaksan EVO-III',
      status: 'in_progress',
      setup_time_hours: 0.25,
      run_time_hours: 1,
      component_quantity: 18,
    },
    {
      id: 103,
      sequence: 30,
      operation_number: 'OP30',
      name: 'QC FINAL',
      work_center_id: 2,
      work_center_name: 'Inspection',
      status: 'pending',
      setup_time_hours: 0,
      run_time_hours: 0.5,
    },
  ],
};

const PART: Part = {
  id: 9,
  version: 0,
  part_number: 'M3Z-24100048-BRAKE-SET',
  name: 'Brake set',
  revision: 'A',
  drawing_number: 'DWG-9',
  unit_of_measure: 'each',
  part_type: 'manufactured',
  standard_cost: 12.5,
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

/**
 * The routing row for an operation, looked up by the number the traveler PRINTS.
 *
 * The fixtures above store the LEGACY spelling (`OP10`) that the create form used
 * to mint; the traveler's "Op #" column now prints the bare identifier (`10`), so
 * the argument here is deliberately the bare form. Passing the stored string
 * through `operationNumberText` inside this helper would make the lookup circular
 * -- it would find the row whatever the page rendered.
 */
function routingRow(printedNumber: string): HTMLElement {
  // Scoped to the routing table (the one carrying the "Op #" header): the traveler
  // also prints the ORDER QUANTITY, which collides with a bare "10" now that the
  // column no longer prints the "OP" prefix.
  const routingTable = screen.getByRole('columnheader', { name: 'Op #' }).closest('table');
  if (!routingTable) throw new Error('no routing table');
  const cell = within(routingTable as HTMLElement).getByText(printedNumber);
  const row = cell.closest('tr');
  if (!row) throw new Error(`no routing row for ${printedNumber}`);
  return row as HTMLElement;
}

describe('PrintTraveler — per-operation required quantity', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getPart.mockResolvedValue(PART);
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-20260812-003',
      quantity_ordered: 8,
      has_bom: false,
      materials: [],
    });
    mockedToDataURL.mockImplementation(async (payload: string) => `data:image/png;base64,${encodeURIComponent(payload)}`);
  });

  it('prints each pool line item\'s OWN piece count, not the work-order set quantity', async () => {
    mockedApi.getWorkOrder.mockResolvedValue(POOL_WORK_ORDER);
    renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());

    // Each line item builds to its own count — these ops carry no component part number.
    expect(within(routingRow('10')).getByText('Qty: 8')).toBeInTheDocument();
    expect(within(routingRow('20')).getByText('Qty: 18')).toBeInTheDocument();
    // The whole-order QC step has no target of its own, so it inherits the WO quantity.
    expect(within(routingRow('30')).getByText('Qty: 8')).toBeInTheDocument();
  });

  it('still prints the component identity for a BOM component operation, with its target', async () => {
    mockedApi.getWorkOrder.mockResolvedValue({
      ...POOL_WORK_ORDER,
      quantity_ordered: 10,
      operations: [
        {
          ...POOL_WORK_ORDER.operations[0],
          name: 'BRK-100 - Bend',
          component_part_number: 'BRK-100',
          component_part_name: 'Bracket',
          component_quantity: 40,
        },
      ],
    });
    renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());

    const row = routingRow('10');
    expect(within(row).getByText('BRK-100')).toBeInTheDocument();
    expect(within(row).getByText('Bracket')).toBeInTheDocument();
    expect(within(row).getByText('Qty: 40')).toBeInTheDocument();
  });
});
