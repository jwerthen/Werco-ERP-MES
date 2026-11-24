/**
 * New Work Order keeps process sheets when the planner edits a step (wo_change/2).
 *
 * Editing any routed row (or restoring a draft) makes the form send its own operation
 * list. Each row that came from the routing must carry `routing_operation_id`, which is
 * what makes the server snapshot that step's process sheet. A removed sheet-bearing step
 * is allowed but warned about, and a restored draft's stale link is shown to the planner
 * (unlink or reload), never silently dropped.
 */
import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import WorkOrderNew from './WorkOrderNew';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { Part, WorkCenter } from '../types';
import type { WorkOrderOperationsPreview } from '../types/workOrderProcessSheet';

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

// The draft hook talks to the workspace-records API; the restore callback is what is
// under test, so capture it and hand back an idle, unblocked draft.
const mockDraft: { restore: ((data: unknown) => void) | null } = { restore: null };
jest.mock('../hooks/useResumableDraft', () => ({
  useResumableDraft: (options: { restore: (data: unknown) => void }) => {
    mockDraft.restore = options.restore;
    return { blocked: false, clear: async () => true };
  },
}));
jest.mock('../components/ui/DraftRecovery', () => ({ DraftRecovery: () => null }));

const mockedApi = api as jest.Mocked<typeof api>;

const WORK_CENTER: WorkCenter = {
  id: 5,
  version: 1,
  code: 'WC-WELD',
  name: 'Weld Cell',
  work_center_type: 'fabrication',
  hourly_rate: 95,
  capacity_hours_per_day: 8,
  efficiency_factor: 0.85,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const PART: Part = {
  id: 1,
  version: 1,
  part_number: 'PN-4410',
  revision: 'A',
  name: 'Frame, welded',
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

const ROUTING = {
  id: 3,
  part_id: 1,
  revision: 'B',
  status: 'released',
  operations: [
    {
      id: 30,
      sequence: 10,
      operation_number: '10',
      name: 'Cut',
      work_center_id: 5,
      work_center: { id: 5, code: 'WC-WELD', name: 'Weld Cell' },
      setup_hours: 0.5,
      run_hours_per_unit: 0.1,
      process_sheet_id: null,
    },
    {
      id: 31,
      sequence: 20,
      operation_number: '20',
      name: 'Weld',
      work_center_id: 5,
      work_center: { id: 5, code: 'WC-WELD', name: 'Weld Cell' },
      setup_hours: 0.5,
      run_hours_per_unit: 0.2,
      process_sheet_id: 7,
    },
  ],
};

const PART_PREVIEW: WorkOrderOperationsPreview = {
  bom_found: false,
  operations_preview: [
    {
      name: 'Cut',
      work_center_id: 5,
      routing_operation_id: 30,
      sequence: 10,
      operation_number: '10',
      component_part_id: null,
      process_sheet_id: null,
      process_sheet_number: null,
      process_sheet_released: null,
    },
    {
      name: 'Weld',
      work_center_id: 5,
      routing_operation_id: 31,
      sequence: 20,
      operation_number: '20',
      component_part_id: null,
      process_sheet_id: 7,
      process_sheet_number: 'PS-104',
      process_sheet_released: true,
    },
  ],
};

beforeEach(() => {
  jest.clearAllMocks();
  mockDraft.restore = null;
  mockedApi.getParts.mockResolvedValue([PART, ASSEMBLY]);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([WORK_CENTER]);
  mockedApi.getCustomerNames.mockResolvedValue([]);
  mockedApi.getPartReadiness.mockResolvedValue({ ready: true, blockers: [], warnings: [], checks: {} });
  mockedApi.getRoutingByPart.mockResolvedValue(ROUTING);
  mockedApi.previewWorkOrderOperations.mockResolvedValue(PART_PREVIEW);
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
  fireEvent.change(screen.getByRole('combobox', { name: /^Part/ }), { target: { value: partNumber } });
  const option = await screen.findByRole('option', { name: new RegExp(partNumber, 'i') });
  fireEvent.mouseDown(option);
  await waitFor(() => expect(mockedApi.getPartReadiness).toHaveBeenCalledWith(partId));
}

function submitButton() {
  return screen.getByRole('button', { name: /create work order/i });
}

type PostedOperation = Record<string, unknown>;
function postedOperations(call = 0): PostedOperation[] {
  return (mockedApi.createWorkOrder.mock.calls[call][0] as { operations: PostedOperation[] }).operations;
}

describe('routed rows keep their process sheet', () => {
  it('sends routing_operation_id on every edited routed row and shows the sheet', async () => {
    await renderPage();
    await selectPart('PN-4410', 1);
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    expect(screen.getByText('Process sheet PS-104')).toBeInTheDocument();

    fireEvent.change(screen.getAllByLabelText('Operation name')[1], { target: { value: 'Weld (revised)' } });
    fireEvent.click(submitButton());
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());

    expect(postedOperations().map(op => op.routing_operation_id)).toEqual([30, 31]);
    expect(screen.queryByTestId('wo-missing-sheet-steps')).not.toBeInTheDocument();
  });

  it('still links rows by the routing id when the preview read fails', async () => {
    mockedApi.previewWorkOrderOperations.mockRejectedValue(new Error('Offline'));
    await renderPage();
    await selectPart('PN-4410', 1);
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    fireEvent.change(screen.getAllByLabelText('Operation name')[0], { target: { value: 'Cut (revised)' } });
    fireEvent.click(submitButton());
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    expect(postedOperations().map(op => op.routing_operation_id)).toEqual([30, 31]);
  });

  it('warns, naming the step and sheet, when a sheet-bearing step is removed, and still creates', async () => {
    await renderPage();
    await selectPart('PN-4410', 1);
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove operation' })[1]);
    const warning = await screen.findByTestId('wo-missing-sheet-steps');
    expect(within(warning).getByText(/Op 20 — Weld \(sheet PS-104\)/)).toBeInTheDocument();

    fireEvent.click(submitButton());
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    expect(postedOperations()).toHaveLength(1);
    expect(postedOperations()[0]).toMatchObject({ name: 'Cut', routing_operation_id: 30 });
  });

  it("sends the routing id and component on an assembly's BOM-derived rows", async () => {
    mockedApi.previewWorkOrderOperations.mockResolvedValue({
      bom_found: true,
      operations_preview: [
        {
          name: 'PN-77 - Cut',
          work_center_id: 5,
          setup_hours: 0.25,
          run_hours_per_unit: 0.05,
          component_part_id: 77,
          component_quantity: 4,
          routing_operation_id: 40,
          sequence: 10,
          operation_number: '10',
          process_sheet_id: 9,
          process_sheet_number: 'PS-200',
          process_sheet_released: false,
        },
        {
          name: 'Assemble',
          work_center_id: 5,
          setup_hours: 0.5,
          run_hours_per_unit: 0.1,
          component_part_id: null,
          routing_operation_id: 41,
          sequence: 20,
          operation_number: '20',
          process_sheet_id: null,
          process_sheet_number: null,
          process_sheet_released: null,
        },
      ],
    });
    await renderPage();
    await selectPart('PN-ASSY', 2);
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    // A sheet with no released revision is warned about before submit.
    expect(screen.getByText(/Creating this work order will be refused until Quality releases/)).toBeInTheDocument();

    fireEvent.change(screen.getAllByLabelText('Operation name')[1], { target: { value: 'Assemble (revised)' } });
    fireEvent.click(submitButton());
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    expect(postedOperations()).toEqual([
      expect.objectContaining({ routing_operation_id: 40, component_part_id: 77, operation_number: '10' }),
      expect.objectContaining({ routing_operation_id: 41, operation_number: '20' }),
    ]);
  });

  it('does not send a routing id on a hand-added row', async () => {
    await renderPage();
    await selectPart('PN-4410', 1);
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    fireEvent.click(screen.getByRole('button', { name: /add operation/i }));
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(3));
    fireEvent.change(screen.getAllByLabelText('Operation name')[2], { target: { value: 'Deburr' } });
    fireEvent.click(submitButton());
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    expect(postedOperations()[2].routing_operation_id).toBeUndefined();
  });
});

describe('a refused link is shown, never dropped', () => {
  it('marks the rows the server refused 409 ROUTING_OPERATION_UNMATCHED and lets the planner unlink them', async () => {
    mockedApi.createWorkOrder.mockRejectedValueOnce({
      response: {
        status: 409,
        data: {
          detail: {
            code: 'ROUTING_OPERATION_UNMATCHED',
            detail:
              "Cannot create work order: operation(s) 20 are linked to a routing step that is not on this part's released routing.",
            operations: [
              { sequence: 20, operation_number: '20', routing_operation_id: 31, reason: 'not_on_released_routing' },
            ],
          },
        },
      },
    });
    await renderPage();
    await selectPart('PN-4410', 1);
    await waitFor(() => expect(screen.getAllByLabelText('Operation name')).toHaveLength(2));
    fireEvent.change(screen.getAllByLabelText('Operation name')[1], { target: { value: 'Weld (revised)' } });
    fireEvent.click(submitButton());

    expect(await screen.findByText(/operation\(s\) 20 are linked to a routing step/)).toBeInTheDocument();
    const stale = await screen.findByTestId('wo-stale-routing-links');
    expect(within(stale).getByText(/Op 20 — Weld \(revised\)/)).toBeInTheDocument();

    // Blocked until resolved.
    fireEvent.click(submitButton());
    expect(mockedApi.createWorkOrder).toHaveBeenCalledTimes(1);

    fireEvent.click(within(stale).getByRole('button', { name: 'Unlink these operations' }));
    await waitFor(() => expect(screen.queryByTestId('wo-stale-routing-links')).not.toBeInTheDocument());
    // Unlinking leaves the sheet step uncarried, which is warned about.
    expect(screen.getByTestId('wo-missing-sheet-steps')).toBeInTheDocument();

    fireEvent.click(submitButton());
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalledTimes(2));
    expect(postedOperations(1).map(op => op.routing_operation_id)).toEqual([30, undefined]);
  });

  it("checks a restored draft against today's routing and warns about a stale link", async () => {
    await renderPage();
    expect(mockDraft.restore).not.toBeNull();
    act(() => {
      mockDraft.restore?.({
        form: {
          part_id: 1,
          quantity_ordered: 1,
          priority: 5,
          customer_name: '',
          customer_po: '',
          unit_number: '',
          due_date: '',
          notes: '',
        },
        operations: [
          {
            sequence: 10,
            operation_number: '10',
            name: 'Cut',
            work_center_id: 5,
            work_center_name: 'Weld Cell',
            setup_time_hours: 0,
            run_time_hours: 0,
            fromRouting: true,
            routing_operation_id: 30,
          },
          {
            sequence: 20,
            operation_number: '20',
            name: 'Old weld',
            work_center_id: 5,
            work_center_name: 'Weld Cell',
            setup_time_hours: 0,
            run_time_hours: 0,
            fromRouting: true,
            routing_operation_id: 99,
            process_sheet_id: 7,
            process_sheet_number: 'PS-104',
          },
        ],
        operationsOverridden: true,
        showManualEntry: true,
        serialsText: '',
      });
    });

    await waitFor(() => expect(mockedApi.previewWorkOrderOperations).toHaveBeenCalledWith(1, 1));
    const stale = await screen.findByTestId('wo-stale-routing-links');
    expect(within(stale).getByText(/Op 20 — Old weld/)).toBeInTheDocument();
    // Today's Weld step (31) carries a sheet and is not linked by any restored row.
    expect(within(screen.getByTestId('wo-missing-sheet-steps')).getByText(/Weld \(sheet PS-104\)/)).toBeInTheDocument();

    fireEvent.click(submitButton());
    await screen.findByText(/no longer on the released routing\. Unlink them or reload/);
    expect(mockedApi.createWorkOrder).not.toHaveBeenCalled();
    // Both rows are still there.
    expect(screen.getAllByLabelText('Operation name')).toHaveLength(2);
  });
});
