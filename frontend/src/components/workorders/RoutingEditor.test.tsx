/**
 * RoutingEditor — changing a released work order's routing (owner decision 2026-11-25).
 *
 * Every action is server-GATED and NON-optimistic: the request carries the operation's
 * `version` and the person's reason, the work order is re-read only after the server accepted
 * the change, and a refusal is shown verbatim with the dialog left open.
 *
 * Fixtures are typed `WorkOrder` / `WorkOrderOperation` and the mocks resolve the real
 * api.ts return shapes, so tsconfig.test.json catches a drifted contract.
 */
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import RoutingEditor from './RoutingEditor';
import type {
  OperationsResequenceResponse,
  RoutingChangeResponse,
  WorkCenter,
  WorkOrder,
  WorkOrderOperation,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    skipOperation: jest.fn(),
    unskipOperation: jest.fn(),
    resequenceOperations: jest.fn(),
    addOperation: jest.fn(),
    updateOperation: jest.fn(),
    getWorkCenters: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

function op(overrides: Partial<WorkOrderOperation> = {}): WorkOrderOperation {
  return {
    id: 1,
    version: 1,
    work_order_id: 42,
    work_center_id: 9,
    work_center_name: 'Weld Cell',
    sequence: 10,
    operation_number: '10',
    name: 'Fit Up',
    setup_time_hours: 0.5,
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

const OPERATIONS: WorkOrderOperation[] = [
  op({
    id: 11,
    version: 4,
    sequence: 10,
    operation_number: '10',
    name: 'Saw',
    status: 'complete',
    quantity_complete: 2,
  }),
  op({ id: 12, version: 2, sequence: 20, operation_number: '20', name: 'Drill', status: 'ready' }),
  op({ id: 13, version: 7, sequence: 30, operation_number: '30', name: 'Deburr', status: 'pending' }),
  op({
    id: 14,
    version: 3,
    sequence: 40,
    operation_number: '40',
    name: 'Paint',
    status: 'skipped',
    skip_reason: 'Customer supplies finish',
    skipped_by: 5,
    skipped_at: '2026-11-25T15:00:00Z',
  }),
];

function workOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    id: 42,
    version: 3,
    work_order_number: 'WO-1',
    part_id: 100,
    work_order_type: 'production',
    sequential_operations: true,
    quantity_ordered: 2,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 4,
    actual_hours: 0,
    created_at: '2026-11-25T00:00:00Z',
    updated_at: '2026-11-25T00:00:00Z',
    operations: OPERATIONS,
    ...overrides,
  };
}

const WORK_CENTERS: WorkCenter[] = [
  {
    id: 9,
    version: 1,
    code: 'WELD-1',
    name: 'Weld Cell',
    work_center_type: 'welding',
    hourly_rate: 80,
    capacity_hours_per_day: 8,
    efficiency_factor: 1,
    is_active: true,
    current_status: 'available',
    created_at: '2026-11-25T00:00:00Z',
  } as WorkCenter,
];

function routingChange(operation: WorkOrderOperation): RoutingChangeResponse {
  return { operation, promoted_operation_ids: [], demoted_operation_ids: [], work_order_status: 'released' };
}

function httpError(status: number, detail: string) {
  return { response: { status, data: { detail } } };
}

function renderEditor(wo: WorkOrder = workOrder(), onChanged = jest.fn().mockResolvedValue(undefined)) {
  const utils = render(
    <ToastProvider>
      <RoutingEditor workOrder={wo} onChanged={onChanged} onClose={jest.fn()} />
    </ToastProvider>
  );
  return { ...utils, onChanged };
}

const step = (id: number) => screen.getByTestId(`routing-step-${id}`);

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getWorkCenters.mockResolvedValue(WORK_CENTERS);
});

describe('per-step controls', () => {
  it('offers Skip only on a not-started step, Restore only on a skipped one, Edit not on complete/skipped', () => {
    renderEditor();
    expect(within(step(11)).queryByRole('button', { name: 'Skip Op 10' })).not.toBeInTheDocument();
    expect(within(step(11)).queryByRole('button', { name: 'Edit Op 10' })).not.toBeInTheDocument();
    expect(within(step(12)).getByRole('button', { name: 'Skip Op 20' })).toBeEnabled();
    expect(within(step(12)).getByRole('button', { name: 'Edit Op 20' })).toBeInTheDocument();
    expect(within(step(14)).getByRole('button', { name: 'Restore Op 40' })).toBeInTheDocument();
    expect(within(step(14)).queryByRole('button', { name: 'Edit Op 40' })).not.toBeInTheDocument();
  });

  it('disables Skip on a pending step that already has labor recorded', () => {
    renderEditor(
      workOrder({
        operations: [
          OPERATIONS[0],
          op({ id: 12, sequence: 20, operation_number: '20', status: 'ready', has_labor_evidence: true }),
        ],
      })
    );
    expect(within(step(12)).getByRole('button', { name: 'Skip Op 20' })).toBeDisabled();
  });

  it('cannot move a not-started step above a completed one', () => {
    renderEditor();
    expect(within(step(12)).getByRole('button', { name: 'Move Op 20 up' })).toBeDisabled();
    expect(within(step(12)).getByRole('button', { name: 'Move Op 20 down' })).toBeEnabled();
  });
});

describe('skip', () => {
  it('requires a reason, sends version + reason, then re-reads the work order', async () => {
    const user = userEvent.setup();
    mockedApi.skipOperation.mockResolvedValue(routingChange({ ...OPERATIONS[1], status: 'skipped' }));
    const { onChanged } = renderEditor();

    await user.click(screen.getByRole('button', { name: 'Skip Op 20' }));
    const dialog = await screen.findByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: 'Skip step' });
    expect(submit).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/Reason/), '  Not needed on this rev  ');
    await user.click(submit);

    await waitFor(() =>
      expect(mockedApi.skipOperation).toHaveBeenCalledWith(12, { version: 2, reason: 'Not needed on this rev' })
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('Op 20 skipped')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows the server refusal verbatim and keeps the dialog open', async () => {
    const user = userEvent.setup();
    const detail = 'Op 20 already has recorded work (a start, pieces or labor) and cannot be skipped.';
    mockedApi.skipOperation.mockRejectedValue(httpError(409, detail));
    const { onChanged } = renderEditor();

    await user.click(screen.getByRole('button', { name: 'Skip Op 20' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/Reason/), 'why');
    await user.click(within(dialog).getByRole('button', { name: 'Skip step' }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // A state refusal is not a stale version: no silent re-read under the open dialog.
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('re-reads the work order on a stale-version refusal', async () => {
    const user = userEvent.setup();
    const detail = 'Operation was modified by someone else. Refresh and try again.';
    mockedApi.skipOperation.mockRejectedValue(httpError(409, detail));
    const { onChanged } = renderEditor();

    await user.click(screen.getByRole('button', { name: 'Skip Op 20' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/Reason/), 'why');
    await user.click(within(dialog).getByRole('button', { name: 'Skip step' }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});

describe('restore', () => {
  it('sends the skipped step version and the reason', async () => {
    const user = userEvent.setup();
    mockedApi.unskipOperation.mockResolvedValue(routingChange({ ...OPERATIONS[3], status: 'pending' }));
    const { onChanged } = renderEditor();

    await user.click(screen.getByRole('button', { name: 'Restore Op 40' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/Reason/), 'Finish back in scope');
    await user.click(within(dialog).getByRole('button', { name: 'Restore step' }));

    await waitFor(() =>
      expect(mockedApi.unskipOperation).toHaveBeenCalledWith(14, { version: 3, reason: 'Finish back in scope' })
    );
    expect(onChanged).toHaveBeenCalled();
  });
});

describe('reorder', () => {
  it('swaps two not-started steps with their versions, never touching operation numbers', async () => {
    const user = userEvent.setup();
    const response: OperationsResequenceResponse = {
      operations: OPERATIONS,
      changed_operation_ids: [12, 13],
      promoted_operation_ids: [],
      demoted_operation_ids: [],
    };
    mockedApi.resequenceOperations.mockResolvedValue(response);
    renderEditor();

    await user.click(screen.getByRole('button', { name: 'Move Op 30 up' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/Reason/), 'Deburr before drilling');
    await user.click(within(dialog).getByRole('button', { name: 'Reorder' }));

    await waitFor(() =>
      expect(mockedApi.resequenceOperations).toHaveBeenCalledWith(42, {
        reason: 'Deburr before drilling',
        operations: [
          { id: 13, version: 7, sequence: 20 },
          { id: 12, version: 2, sequence: 30 },
        ],
      })
    );
  });

  it('moves past a skipped step rather than swapping with it', async () => {
    const user = userEvent.setup();
    mockedApi.resequenceOperations.mockResolvedValue({
      operations: [],
      changed_operation_ids: [],
      promoted_operation_ids: [],
      demoted_operation_ids: [],
    });
    renderEditor(
      workOrder({
        operations: [
          op({ id: 21, version: 1, sequence: 10, operation_number: '10', status: 'ready' }),
          op({ id: 22, version: 1, sequence: 20, operation_number: '20', status: 'skipped' }),
          op({ id: 23, version: 5, sequence: 30, operation_number: '30', status: 'pending' }),
        ],
      })
    );
    await user.click(screen.getByRole('button', { name: 'Move Op 30 up' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/Reason/), 'r');
    await user.click(within(dialog).getByRole('button', { name: 'Reorder' }));
    await waitFor(() =>
      expect(mockedApi.resequenceOperations).toHaveBeenCalledWith(42, {
        reason: 'r',
        operations: [
          { id: 23, version: 5, sequence: 10 },
          { id: 21, version: 1, sequence: 30 },
        ],
      })
    );
  });
});

describe('add step', () => {
  async function fillAdd(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: /Add step/ }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByRole('option', { name: 'WELD-1 — Weld Cell' });
    await user.type(within(dialog).getByLabelText(/^Name/), 'Inspect');
    await user.selectOptions(within(dialog).getByLabelText(/^Work center/), '9');
    return dialog;
  }

  it('requires a reason on a released work order and sends it as change_reason', async () => {
    const user = userEvent.setup();
    mockedApi.addOperation.mockResolvedValue(op({ id: 99, sequence: 50, name: 'Inspect' }));
    const { onChanged } = renderEditor();
    const dialog = await fillAdd(user);

    // 40 is skipped, so the next free sequence is past the highest non-skipped one (30).
    expect(within(dialog).getByLabelText(/^Sequence/)).toHaveValue(40);
    await user.click(within(dialog).getByRole('button', { name: 'Add step' }));
    expect(await within(dialog).findByText('A reason is required to change a released work order')).toBeInTheDocument();
    expect(mockedApi.addOperation).not.toHaveBeenCalled();

    await user.type(within(dialog).getByLabelText(/^Reason for the change/), 'Customer added FAI');
    await user.click(within(dialog).getByRole('button', { name: 'Add step' }));

    await waitFor(() =>
      expect(mockedApi.addOperation).toHaveBeenCalledWith(
        42,
        expect.objectContaining({
          sequence: 40,
          operation_number: '40',
          name: 'Inspect',
          work_center_id: 9,
          change_reason: 'Customer added FAI',
        })
      )
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it('hides the reason on a draft work order and sends none', async () => {
    const user = userEvent.setup();
    mockedApi.addOperation.mockResolvedValue(op({ id: 99 }));
    renderEditor(workOrder({ status: 'draft' }));
    const dialog = await fillAdd(user);

    expect(within(dialog).queryByLabelText(/Reason for the change/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Add step' }));

    await waitFor(() => expect(mockedApi.addOperation).toHaveBeenCalled());
    expect(mockedApi.addOperation.mock.calls[0][1]).not.toHaveProperty('change_reason');
  });

  it('refuses a sequence already used by a step that is not skipped', async () => {
    const user = userEvent.setup();
    renderEditor(workOrder({ status: 'draft' }));
    const dialog = await fillAdd(user);
    const sequence = within(dialog).getByLabelText(/^Sequence/);
    await user.clear(sequence);
    await user.type(sequence, '20');
    await user.click(within(dialog).getByRole('button', { name: 'Add step' }));
    expect(await within(dialog).findByText('Sequence 20 is already used on this work order')).toBeInTheDocument();
    expect(mockedApi.addOperation).not.toHaveBeenCalled();
  });

  it('keeps the dialog open with the server refusal', async () => {
    const user = userEvent.setup();
    const detail = 'Cannot add a step at sequence 40: Op 30 (sequence 30) already has recorded work.';
    mockedApi.addOperation.mockRejectedValue(httpError(409, detail));
    renderEditor(workOrder({ status: 'draft' }));
    const dialog = await fillAdd(user);
    await user.click(within(dialog).getByRole('button', { name: 'Add step' }));
    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});

describe('edit step', () => {
  it('sends only the changed fields with version and the reason', async () => {
    const user = userEvent.setup();
    mockedApi.updateOperation.mockResolvedValue({});
    renderEditor();

    await user.click(screen.getByRole('button', { name: 'Edit Op 30' }));
    const dialog = await screen.findByRole('dialog');
    const name = within(dialog).getByLabelText(/^Name/);
    await user.clear(name);
    await user.type(name, 'Deburr + break edges');
    await user.type(within(dialog).getByLabelText(/^Reason for the change/), 'Drawing note added');
    await user.click(within(dialog).getByRole('button', { name: 'Save step' }));

    await waitFor(() =>
      expect(mockedApi.updateOperation).toHaveBeenCalledWith(13, {
        version: 7,
        name: 'Deburr + break edges',
        change_reason: 'Drawing note added',
      })
    );
  });
});
