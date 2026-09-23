import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { HankIntakeFile, HankIntakeReceivingDraft } from '../../types/hankIntake';
import type { HankOperationalTask } from './HankOperationalTask';
import type { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankDocumentReceiving } from './HankDocumentReceiving';

jest.mock('../../services/api', () => ({ __esModule: true, default: { getHankIntakeReceivingDraft: jest.fn() } }));
jest.mock('./HankSourceFile', () => ({ HankSourceFile: () => <button>Open source PDF</button> }));
jest.mock('./HankPurchaseOrderPicker', () => ({
  HankPurchaseOrderPicker: ({
    id,
    value,
    onChange,
    disabled,
  }: React.ComponentProps<typeof HankPurchaseOrderPicker>) => (
    <select id={id} value={value} onChange={event => onChange(event.target.value)} disabled={disabled}>
      <option value="">Choose PO</option>
      <option value="11">PO-11</option>
      <option value="12">PO-12</option>
    </select>
  ),
}));
jest.mock('./HankOperationalTask', () => ({
  HankOperationalTask: ({
    receivingDraft,
    onBusyChange,
    onRefreshReceiving,
  }: React.ComponentProps<typeof HankOperationalTask>) => (
    <section aria-label="Receipt proposal">
      <p>
        Source {receivingDraft?.file_id} version {receivingDraft?.file_version} for PO{' '}
        {receivingDraft?.purchase_order_id}
      </p>
      <button onClick={() => onBusyChange?.(true)}>Start proposal</button>
      <button onClick={() => onBusyChange?.(false)}>Finish proposal</button>
      <button onClick={onRefreshReceiving}>Start another action</button>
    </section>
  ),
}));
const mocked = jest.mocked(api);
const file = { id: 51, company_id: 4, filename: 'delivery.pdf' } as HankIntakeFile;
const draft: HankIntakeReceivingDraft = {
  file_id: 51,
  company_id: 4,
  filename: 'delivery.pdf',
  file_version: 3,
  purchase_order_id: null,
  purchase_orders: [{ id: 11, po_number: 'PO-11', vendor_name: 'Metal Supply', reason: 'Printed PO number' }],
  packing_slip_number: 'PS-21',
  has_duplicates: true,
  requires_duplicate_acknowledgement: false,
  warnings: ['Confirm the units before receiving.'],
  lines: [
    {
      source_line_index: 0,
      description: 'Steel bar',
      part_number: 'BAR',
      quantity: '12',
      unit_of_measure: 'EA',
      lot_number: 'L-9',
      heat_number: 'H-3',
      confidence: 'high',
      evidence: [{ page: 2, excerpt: 'BAR 12 EA' }],
      po_line_id: null,
      candidates: [
        {
          po_line_id: 31,
          line_number: 1,
          part_id: 1,
          part_number: 'BAR',
          description: 'Steel bar',
          quantity_remaining: 20,
          unit_of_measure: 'EA',
        },
      ],
      quantity_received: null,
      warnings: ['No unique PO line match.'],
    },
  ],
};
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.sig`);
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  mocked.getHankIntakeReceivingDraft.mockResolvedValue(draft);
});
it('shows source evidence and uncertain matches, then loads suggestions for the employee-selected PO', async () => {
  const onBusyChange = jest.fn();
  mocked.getHankIntakeReceivingDraft
    .mockResolvedValueOnce(draft)
    .mockResolvedValue({ ...draft, purchase_order_id: 11 });
  render(<HankDocumentReceiving file={file} onNavigate={jest.fn()} onBusyChange={onBusyChange} />);
  expect(await screen.findByText('Page 2: BAR 12 EA')).toBeInTheDocument();
  expect(screen.getByText('Lot L-9 · Heat H-3')).toBeInTheDocument();
  expect(screen.getByText('No unique PO line match.')).toBeInTheDocument();
  expect(screen.getByText('PO line 1 · BAR · Stocking unit EA · Remaining 20')).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'Receipt proposal' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /PO-11 · Metal Supply/ }));
  expect(await screen.findByText('Source 51 version 3 for PO 11')).toBeInTheDocument();
  expect(mocked.getHankIntakeReceivingDraft).toHaveBeenLastCalledWith(51, 11, expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('button', { name: 'Start proposal' }));
  expect(screen.getByLabelText(/Purchase order for this PDF/)).toBeDisabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
});
it('does not show evidence returned for another company', async () => {
  mocked.getHankIntakeReceivingDraft.mockResolvedValue({ ...draft, company_id: 9 });
  render(<HankDocumentReceiving file={file} onNavigate={jest.fn()} />);
  await waitFor(() => expect(screen.queryByText('Matching the PDF to receiving lines…')).not.toBeInTheDocument());
  expect(screen.queryByText('Page 2: BAR 12 EA')).not.toBeInTheDocument();
});
it('aborts and ignores a late document response after the company changes', async () => {
  let resolve!: (value: HankIntakeReceivingDraft) => void;
  mocked.getHankIntakeReceivingDraft.mockImplementation(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  render(<HankDocumentReceiving file={file} onNavigate={jest.fn()} />);
  session(9);
  await act(async () => {
    resolve(draft);
  });
  expect(screen.queryByText('Page 2: BAR 12 EA')).not.toBeInTheDocument();
});

it('reloads source version and duplicate receipt checks when starting another receipt', async () => {
  mocked.getHankIntakeReceivingDraft.mockResolvedValue({ ...draft, purchase_order_id: 11 });
  render(<HankDocumentReceiving file={file} onNavigate={jest.fn()} />);
  await screen.findByText('Source 51 version 3 for PO 11');
  mocked.getHankIntakeReceivingDraft.mockResolvedValue({
    ...draft,
    file_version: 4,
    purchase_order_id: 11,
    requires_duplicate_acknowledgement: true,
  });
  fireEvent.click(screen.getByRole('button', { name: 'Start another action' }));
  expect(await screen.findByText('Source 51 version 4 for PO 11')).toBeInTheDocument();
  expect(mocked.getHankIntakeReceivingDraft).toHaveBeenCalledTimes(2);
});
