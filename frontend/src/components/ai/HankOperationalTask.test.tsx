import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { HankIntakeReceivingDraft } from '../../types/hankIntake';
import type { HankTask } from '../../types/hankTasks';
import type EntityPicker from '../operations/EntityPicker';
import type { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import type { HankTaskWorkflow } from './HankTaskWorkflow';
import { HankOperationalTask } from './HankOperationalTask';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankCapabilities: jest.fn(),
    getPOForReceiving: jest.fn(),
    getWorkOrder: jest.fn(),
    createHankTask: jest.fn(),
  },
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, disabled, onChange }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">Choose</option>
      <option value="7">WO-7</option>
      <option value="8">WO-8</option>
    </select>
  ),
}));
jest.mock('./HankPurchaseOrderPicker', () => ({
  HankPurchaseOrderPicker: ({
    id,
    value,
    disabled,
    onChange,
  }: React.ComponentProps<typeof HankPurchaseOrderPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">Choose</option>
      <option value="11">PO-11</option>
    </select>
  ),
}));
jest.mock('./HankJobScan', () => ({ HankJobScan: () => null }));
jest.mock('./HankTaskWorkflow', () => ({
  HankTaskWorkflow: ({ initialTask }: React.ComponentProps<typeof HankTaskWorkflow>) => (
    <p>{initialTask?.preview.summary}</p>
  ),
}));
const mocked = jest.mocked(api);
const task: HankTask = {
  id: 41,
  company_id: 4,
  kind: 'receive_delivery',
  title: 'Receive PO-11',
  status: 'awaiting_review',
  version: 1,
  input: {},
  preview: { summary: 'Review actual quantities before posting.', changes: [], warnings: [], references: [] },
  result: null,
  error_message: null,
  created_at: '2026-09-22T10:00:00Z',
  updated_at: '2026-09-22T10:00:00Z',
  completed_at: null,
};
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.sig`);
}
function prepare() {
  fireEvent.click(screen.getByRole('button', { name: 'Prepare for review' }));
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  let uuidSequence = 0;
  Object.defineProperty(crypto, 'randomUUID', {
    configurable: true,
    value: jest.fn(() => `11111111-1111-4111-8111-${String(++uuidSequence).padStart(12, '0')}`),
  });
  mocked.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_write: true,
    can_watch: true,
    allowed_kinds: ['receive_delivery', 'report_production', 'draft_shipment'],
  });
  mocked.getPOForReceiving.mockResolvedValue({
    po_id: 11,
    po_number: 'PO-11',
    lines: [
      { line_id: 31, line_number: 1, part_number: 'P1', part_name: 'Bracket', quantity_remaining: 20 },
      { line_id: 32, line_number: 2, part_number: 'P2', part_name: 'Plate', quantity_remaining: 30 },
      { line_id: 33, line_number: 3, part_number: 'P3', part_name: 'Closed', quantity_remaining: 0, is_closed: true },
    ],
  });
  mocked.getWorkOrder.mockResolvedValue({
    id: 7,
    work_order_number: 'WO-7',
    operations: [{ id: 71, sequence: 10, name: 'Mill', status: 'in_progress', quantity_complete: 9 }],
  });
  mocked.createHankTask.mockResolvedValue(task);
});

it('requires an explicit inspection decision for actual received lines and omits untouched lines', async () => {
  render(<HankOperationalTask kind="receive_delivery" purchaseOrderId={11} onNavigate={jest.fn()} />);
  const quantity = await screen.findByLabelText('Delivered quantity, line 1');
  expect(quantity).toHaveValue(null);
  expect(screen.queryByLabelText('Delivered quantity, line 3')).not.toBeInTheDocument();
  fireEvent.change(quantity, { target: { value: '3' } });
  prepare();
  await screen.findByText('Enter a valid quantity and choose whether inspection is required.');
  expect(mocked.createHankTask).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Inspection required, line 1'), { target: { value: 'yes' } });
  fireEvent.change(screen.getByLabelText('lot number, line 1'), { target: { value: 'LOT-8' } });
  prepare();
  await screen.findByText(task.preview.summary);
  expect(mocked.createHankTask).toHaveBeenCalledWith(
    {
      expected_company_id: 4,
      request_key: expect.any(String),
      kind: 'receive_delivery',
      input: {
        purchase_order_id: 11,
        lines: [
          {
            po_line_id: 31,
            quantity_received: 3,
            requires_inspection: true,
            lot_number: 'LOT-8',
            over_receive_approved: false,
          },
        ],
      },
    },
    expect.any(AbortSignal)
  );
});

it('labels production choices by their operation number when sequence repeats', async () => {
  mocked.getWorkOrder.mockResolvedValue({
    id: 7,
    work_order_number: 'WO-7',
    operations: [
      { id: 71, sequence: 10, operation_number: '10', name: 'Inlets out', status: 'ready', quantity_complete: 0 },
      { id: 72, sequence: 10, operation_number: '20', name: 'Inlets in', status: 'ready', quantity_complete: 0 },
    ],
  });
  render(<HankOperationalTask kind="report_production" workOrderId={7} onNavigate={jest.fn()} />);
  expect(await screen.findByRole('option', { name: 'Op 10 · Inlets out · ready · complete 0' })).toHaveValue('71');
  expect(screen.getByRole('option', { name: 'Op 20 · Inlets in · ready · complete 0' })).toHaveValue('72');
});

it('requires scrap and hold reasons and saves exactly the reviewed production deltas', async () => {
  render(<HankOperationalTask kind="report_production" workOrderId={7} operationId={71} onNavigate={jest.fn()} />);
  await screen.findByRole('option', { name: /Mill/ });
  fireEvent.change(screen.getByLabelText('Good quantity to add'), { target: { value: '4' } });
  fireEvent.change(screen.getByLabelText('Scrap quantity to add'), { target: { value: '1' } });
  prepare();
  await screen.findByText('Describe why the material was scrapped.');
  expect(mocked.createHankTask).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Scrap reason'), { target: { value: 'Bad edge' } });
  fireEvent.click(screen.getByLabelText('Put this operation on hold'));
  prepare();
  await screen.findByText('Describe the reason for the hold.');
  expect(screen.getByText(/every active employee time entry/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/Hold details/), { target: { value: 'Fixture needs review' } });
  prepare();
  await screen.findByText(task.preview.summary);
  expect(mocked.createHankTask.mock.calls[0][0]).toMatchObject({
    kind: 'report_production',
    input: {
      operation_id: 71,
      quantity_complete_delta: 4,
      quantity_scrapped_delta: 1,
      scrap_reason: 'Bad edge',
      open_ncr: false,
      hold: { category: 'other', severity: 'medium', note: 'Fixture needs review' },
    },
  });
});

it('does not offer a zero-count hold as a production report', async () => {
  render(<HankOperationalTask kind="report_production" workOrderId={7} operationId={71} onNavigate={jest.fn()} />);
  await screen.findByRole('option', { name: /Mill/ });
  fireEvent.click(screen.getByLabelText('Put this operation on hold'));
  fireEvent.change(screen.getByLabelText(/Hold details/), { target: { value: 'Review fixture' } });
  prepare();
  await screen.findByRole('alert');
  expect(mocked.createHankTask).not.toHaveBeenCalled();
});

it('freezes an uncertain shipment proposal and retries the same body and UUID', async () => {
  mocked.createHankTask.mockRejectedValueOnce(new Error('Lost response'));
  render(<HankOperationalTask kind="draft_shipment" workOrderId={7} onNavigate={jest.fn()} />);
  fireEvent.change(await screen.findByLabelText(/Shipment quantity/), { target: { value: '5' } });
  fireEvent.change(screen.getByLabelText('ship to name'), { target: { value: 'Receiving dock' } });
  prepare();
  await screen.findByText(/proposal was not confirmed/);
  expect(screen.getByLabelText(/Shipment quantity/)).toBeDisabled();
  const body = mocked.createHankTask.mock.calls[0][0];
  expect(body).toMatchObject({
    kind: 'draft_shipment',
    input: { work_order_id: 7, quantity_shipped: 5, num_packages: 1, ship_to_name: 'Receiving dock' },
  });
  expect(body.input).not.toHaveProperty('cert_of_conformance');
  fireEvent.click(screen.getByRole('button', { name: 'Retry same proposal' }));
  await screen.findByText(task.preview.summary);
  expect(mocked.createHankTask.mock.calls[1][0]).toEqual(body);
  expect(crypto.randomUUID).toHaveBeenCalledTimes(1);
});

it('uses server action authority and hides a denied workflow', async () => {
  mocked.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_write: true,
    can_watch: false,
    allowed_kinds: ['report_production'],
  });
  render(<HankOperationalTask kind="draft_shipment" onNavigate={jest.fn()} />);
  await screen.findByText(/unavailable for your current role/);
  expect(screen.queryByRole('button', { name: 'Prepare for review' })).not.toBeInTheDocument();
  expect(mocked.createHankTask).not.toHaveBeenCalled();
});

it('aborts an in-flight proposal on session change and suppresses a late review', async () => {
  let resolve!: (value: HankTask) => void;
  mocked.createHankTask.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  render(<HankOperationalTask kind="draft_shipment" workOrderId={7} onNavigate={jest.fn()} />);
  fireEvent.change(await screen.findByLabelText(/Shipment quantity/), { target: { value: '5' } });
  prepare();
  await waitFor(() => expect(mocked.createHankTask).toHaveBeenCalledTimes(1));
  const signal = mocked.createHankTask.mock.calls[0][1];
  session(5);
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(task));
  expect(screen.queryByText(task.preview.summary)).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('session changed');
});

const receivingDraft: HankIntakeReceivingDraft = {
  file_id: 51,
  file_version: 3,
  company_id: 4,
  filename: 'delivery.pdf',
  purchase_order_id: 11,
  purchase_orders: [],
  packing_slip_number: 'PS-12',
  warnings: [],
  has_duplicates: false,
  requires_duplicate_acknowledgement: false,
  lines: [
    {
      source_line_index: 0,
      description: 'Bracket',
      part_number: 'P1',
      quantity: '3',
      unit_of_measure: 'EA',
      lot_number: 'LOT-8',
      heat_number: 'H-10',
      confidence: 'high',
      evidence: [{ page: 1, excerpt: 'P1 qty 3' }],
      po_line_id: 31,
      candidates: [],
      quantity_received: 3,
      warnings: [],
    },
  ],
};

it('prefills matched PDF quantities and traceability but requires the employee inspection decision', async () => {
  render(
    <HankOperationalTask
      kind="receive_delivery"
      purchaseOrderId={11}
      receivingDraft={receivingDraft}
      onNavigate={jest.fn()}
    />
  );
  expect(await screen.findByLabelText('Delivered quantity, line 1')).toHaveValue(3);
  expect(screen.getByLabelText('Delivered quantity, line 2')).toHaveValue(null);
  expect(screen.getByLabelText('Inspection required, line 1')).toHaveValue('');
  expect(screen.getByLabelText('lot number, line 1')).toHaveValue('LOT-8');
  expect(screen.getByLabelText('heat number, line 1')).toHaveValue('H-10');
  expect(screen.getByLabelText('packing slip number, line 1')).toHaveValue('PS-12');
  expect(screen.getByLabelText(/Purchase order/)).toBeDisabled();
  prepare();
  await screen.findByText('Enter a valid quantity and choose whether inspection is required.');
  expect(mocked.createHankTask).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Inspection required, line 1'), { target: { value: 'yes' } });
  prepare();
  await screen.findByText(task.preview.summary);
  expect(mocked.createHankTask).toHaveBeenCalledWith(
    expect.objectContaining({
      input: expect.objectContaining({
        source_intake_file_id: 51,
        source_intake_version: 3,
        acknowledge_duplicate_source: false,
        lines: [
          expect.objectContaining({
            po_line_id: 31,
            quantity_received: 3,
            requires_inspection: true,
            lot_number: 'LOT-8',
            heat_number: 'H-10',
            packing_slip_number: 'PS-12',
          }),
        ],
      }),
    }),
    expect.any(AbortSignal)
  );
});

it('does not combine distinct document lots on the same PO line', async () => {
  const draft = {
    ...receivingDraft,
    lines: [...receivingDraft.lines, { ...receivingDraft.lines[0], source_line_index: 1, lot_number: 'OTHER' }],
  };
  render(
    <HankOperationalTask kind="receive_delivery" purchaseOrderId={11} receivingDraft={draft} onNavigate={jest.fn()} />
  );
  expect(await screen.findByLabelText('Delivered quantity, line 1')).toHaveValue(null);
  expect(screen.getByLabelText('lot number, line 1')).toHaveValue('');
});

it('requires duplicate receipt acknowledgement for a previously received PDF', async () => {
  render(
    <HankOperationalTask
      kind="receive_delivery"
      purchaseOrderId={11}
      receivingDraft={{ ...receivingDraft, requires_duplicate_acknowledgement: true }}
      onNavigate={jest.fn()}
    />
  );
  fireEvent.change(await screen.findByLabelText('Inspection required, line 1'), { target: { value: 'yes' } });
  prepare();
  await screen.findByText('Review the prior receipt and confirm this is an additional delivery before proceeding.');
  expect(mocked.createHankTask).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the prior receipt/ }));
  prepare();
  await screen.findByText(task.preview.summary);
  expect(mocked.createHankTask.mock.calls[0][0].input).toMatchObject({ acknowledge_duplicate_source: true });
});
