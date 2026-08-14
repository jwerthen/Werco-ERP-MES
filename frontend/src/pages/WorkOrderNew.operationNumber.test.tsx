/**
 * WorkOrderNew stores an operation IDENTIFIER, not a display label.
 *
 * The root cause of the "Op Op 10" defect: `operation_number` is an identifier
 * column, but this form minted `Op ${sequence}` into it. Every screen that then
 * printed the value under its own `Op #` header showed the prefix twice — the
 * kiosk read "Op Op 10" on WO-20260807-006. PR #227 normalized the DISPLAY side;
 * this locks the WRITE side so new rows stop being born with the prefix.
 *
 * There are three mint sites on this page and they are exercised separately
 * because they feed different flows: a manually added operation, an operation
 * copied from the part's routing, and an assembly's BOM-derived preview.
 *
 * The routing-copy case also pins the deliberate NON-transform: a routing's own
 * `operation_number` crosses over VERBATIM. Only the minted fallback changed —
 * normalizing a value the office typed by hand would be this form rewriting
 * stored data on someone else's record.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import WorkOrderNew from './WorkOrderNew';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { Part, WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getParts: jest.fn(),
    getBOMs: jest.fn(),
    getWorkCenters: jest.fn(),
    getCustomerNames: jest.fn(),
    getPartReadiness: jest.fn(),
    getRoutingByPart: jest.fn(),
    previewWorkOrderOperations: jest.fn(),
    createWorkOrder: jest.fn(),
    createCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const WORK_CENTER: WorkCenter = {
  id: 5,
  version: 1,
  code: 'WC-LASER',
  name: 'Laser Cell',
  work_center_type: 'fabrication',
  hourly_rate: 95,
  capacity_hours_per_day: 8,
  efficiency_factor: 0.85,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

/** A full Part, exactly as `api.getParts()` resolves it. */
const PART: Part = {
  id: 1,
  version: 1,
  part_number: 'PN-7731',
  revision: 'A',
  name: 'Bracket, hinge',
  part_type: 'manufactured',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const ASSEMBLY: Part = { ...PART, id: 2, part_number: 'PN-ASSY', part_type: 'assembly' };

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getParts.mockResolvedValue([PART, ASSEMBLY]);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([WORK_CENTER]);
  mockedApi.getCustomerNames.mockResolvedValue([]);
  mockedApi.getPartReadiness.mockResolvedValue({ ready: true, blockers: [], warnings: [], checks: {} });
  mockedApi.getRoutingByPart.mockResolvedValue(null);
  mockedApi.createWorkOrder.mockResolvedValue({ id: 900 });
});

async function renderPage() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/new']}>
        <WorkOrderNew />
      </MemoryRouter>
    </ToastProvider>
  );
  await screen.findByTestId('wo-serial-numbers');
}

async function selectPart(partNumber: string, partId: number) {
  fireEvent.change(screen.getByRole('combobox'), { target: { value: partNumber } });
  const option = await screen.findByRole('option', { name: new RegExp(partNumber, 'i') });
  fireEvent.mouseDown(option);
  await waitFor(() => expect(mockedApi.getPartReadiness).toHaveBeenCalledWith(partId));
}

/**
 * Operations copied from a routing/BOM preview are only POSTed once the planner
 * has touched one (that is what flips `fromRouting` off and arms the payload).
 * Renaming an operation is the lightest edit that does it, and it leaves
 * `operation_number` alone — which is the field under test.
 */
function editOperationName(index: number, name: string) {
  const inputs = screen.getAllByLabelText('Operation name');
  fireEvent.change(inputs[index], { target: { value: name } });
}

async function submit() {
  fireEvent.click(screen.getByRole('button', { name: /create work order/i }));
  await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
  return (mockedApi.createWorkOrder.mock.calls[0][0] as { operations: Array<Record<string, unknown>> })
    .operations;
}

describe('WorkOrderNew posts a bare operation identifier', () => {
  it('mints a manually added operation as "10" — the sequence, not "Op 10"', async () => {
    await renderPage();
    await selectPart('PN-7731', 1);

    fireEvent.click(await screen.findByRole('button', { name: /add first operation/i }));
    editOperationName(0, 'Deburr');

    const operations = await submit();
    expect(operations).toHaveLength(1);
    expect(operations[0].operation_number).toBe('10');
    // The regression itself, stated directly.
    expect(operations[0].operation_number).not.toBe('Op 10');
    expect(String(operations[0].operation_number)).not.toMatch(/op/i);
  });

  it('keeps minting bare identifiers as more operations are added', async () => {
    await renderPage();
    await selectPart('PN-7731', 1);

    fireEvent.click(await screen.findByRole('button', { name: /add first operation/i }));
    editOperationName(0, 'Deburr');
    fireEvent.click(screen.getByRole('button', { name: /add operation/i }));
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    editOperationName(1, 'Inspect');

    const operations = await submit();
    expect(operations.map((op) => op.operation_number)).toEqual(['10', '20']);
  });

  it('mints "20" for a routing operation whose own number is blank', async () => {
    mockedApi.getRoutingByPart.mockResolvedValue({
      id: 3,
      part_id: 1,
      revision: 'A',
      status: 'released',
      operations: [
        {
          id: 30,
          sequence: 20,
          operation_number: null,
          name: 'Laser Cut',
          work_center_id: 5,
          work_center: { id: 5, code: 'WC-LASER', name: 'Laser Cell' },
          setup_hours: 0.5,
          run_hours_per_unit: 0.1,
        },
      ],
    });
    await renderPage();
    await selectPart('PN-7731', 1);

    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(1));
    editOperationName(0, 'Laser Cut (revised)');

    const operations = await submit();
    expect(operations[0].operation_number).toBe('20');
  });

  it('copies a routing operation\'s OWN number verbatim — legacy prefix and all', async () => {
    // Deliberate non-transform. The routing operation and the work-order
    // operation derived from it must agree; rewriting it here would make the
    // pair disagree and would silently edit a number a planner typed.
    mockedApi.getRoutingByPart.mockResolvedValue({
      id: 3,
      part_id: 1,
      revision: 'A',
      status: 'released',
      operations: [
        {
          id: 31,
          sequence: 30,
          operation_number: 'Op 30',
          name: 'Weld',
          work_center_id: 5,
          work_center: { id: 5, code: 'WC-LASER', name: 'Laser Cell' },
          setup_hours: 0.5,
          run_hours_per_unit: 0.1,
        },
      ],
    });
    await renderPage();
    await selectPart('PN-7731', 1);

    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(1));
    editOperationName(0, 'Weld (revised)');

    const operations = await submit();
    expect(operations[0].operation_number).toBe('Op 30');
  });

  it('mints bare identifiers for an assembly\'s BOM-derived operations', async () => {
    mockedApi.previewWorkOrderOperations.mockResolvedValue({
      bom_found: true,
      operations_preview: [
        {
          name: 'Cut component',
          work_center_id: 5,
          work_center_name: 'Laser Cell',
          setup_hours: 0.25,
          run_hours_per_unit: 0.05,
          component_part_id: 77,
          component_quantity: 4,
        },
        {
          name: 'Assemble',
          work_center_id: 5,
          work_center_name: 'Laser Cell',
          setup_hours: 0.5,
          run_hours_per_unit: 0.1,
        },
      ],
    });
    await renderPage();
    await selectPart('PN-ASSY', 2);

    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    editOperationName(0, 'Cut component (revised)');

    const operations = await submit();
    expect(operations.map((op) => op.operation_number)).toEqual(['10', '20']);
    operations.forEach((op) => expect(String(op.operation_number)).not.toMatch(/op/i));
  });
});
