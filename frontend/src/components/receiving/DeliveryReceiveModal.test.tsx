import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DeliveryReceiveModal, { pendingDelivery } from './DeliveryReceiveModal';
import api from '../../services/api';
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    receiveDelivery: jest.fn(),
    uploadReceivingCertificate: jest.fn(),
    downloadDocument: jest.fn(),
    getDocument: jest.fn(),
  },
}));
jest.mock('../../hooks/useUnsavedChanges', () => ({
  __esModule: true,
  default: () => ({ confirmDiscard: () => true, markSaved: jest.fn() }),
}));
const mocked = api as jest.Mocked<typeof api>;
const po = {
  po_id: 10,
  po_number: 'PO-10',
  vendor_name: 'Supplier',
  lines: [
    { line_id: 11, part_number: 'P-ONE', part_name: 'First part', quantity_remaining: 10 },
    { line_id: 12, part_number: 'P-TWO', part_name: 'Second part', quantity_remaining: 4, requires_inspection: true },
  ],
};
const result = {
  batch_id: 1,
  idempotency_key: 'same-key',
  receipts: [
    { id: 1, receipt_number: 'RCV-ONE', quantity_received: 2, lot_number: 'LOT-ONE' },
    { id: 2, receipt_number: 'RCV-TWO', quantity_received: 4, lot_number: 'LOT-TWO' },
  ],
};
const show = () => render(<DeliveryReceiveModal po={po} locations={[]} onClose={jest.fn()} onSaved={jest.fn()} />);
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 2 }));
});
function selectFirst() {
  const line = within(screen.getByRole('group', { name: 'P-ONE · First part' }));
  fireEvent.click(line.getByLabelText(/Receive this line/));
  return line;
}
async function reviewAndPost() {
  fireEvent.click(screen.getByRole('button', { name: 'Review delivery' }));
  fireEvent.click(screen.getByRole('button', { name: /Receive \d lines/ }));
}

test('reviews and posts selected lines with separate lots, heat, certificate and inspection', async () => {
  mocked.receiveDelivery.mockResolvedValue(result);
  mocked.uploadReceivingCertificate.mockResolvedValue({ id: 99, file_name: 'heat.pdf', document_number: 'DOC-99' });
  show();
  const one = selectFirst();
  const two = within(screen.getByRole('group', { name: 'P-TWO · Second part' }));
  fireEvent.click(two.getByLabelText(/Receive this line/));
  fireEvent.change(one.getByLabelText('Quantity received'), { target: { value: '2' } });
  fireEvent.change(one.getByLabelText('lot number'), { target: { value: 'LOT-ONE' } });
  fireEvent.change(one.getByLabelText('heat number'), { target: { value: 'HEAT-ONE' } });
  fireEvent.change(two.getByLabelText('lot number'), { target: { value: 'LOT-TWO' } });
  fireEvent.click(two.getByLabelText('Requires inspection'));
  fireEvent.change(one.getByLabelText(/Certificate file/), {
    target: { files: [new File(['%PDF'], 'heat.pdf', { type: 'application/pdf' })] },
  });
  await one.findByText('Stored: heat.pdf');
  await reviewAndPost();
  await screen.findByText('Delivery received: 2 receipt records.');
  const payload = mocked.receiveDelivery.mock.calls[0][0];
  expect(payload.lines).toEqual([
    expect.objectContaining({
      po_line_id: 11,
      quantity_received: 2,
      lot_number: 'LOT-ONE',
      heat_number: 'HEAT-ONE',
      certificate_document_id: 99,
      requires_inspection: false,
    }),
    expect.objectContaining({ po_line_id: 12, lot_number: 'LOT-TWO', requires_inspection: true }),
  ]);
  expect(pendingDelivery()).toBeNull();
});

test('known atomic rejection keeps line entries editable', async () => {
  mocked.receiveDelivery.mockRejectedValue({
    response: { status: 400, data: { detail: 'Delivery line 1 changed. No lines posted.' } },
  });
  show();
  const one = selectFirst();
  fireEvent.change(one.getByLabelText('lot number'), { target: { value: 'KEEP-LOT' } });
  await reviewAndPost();
  await screen.findByRole('alert');
  expect(screen.getByLabelText('lot number')).toHaveValue('KEEP-LOT');
  expect(screen.getByRole('button', { name: 'Review delivery' })).toBeEnabled();
  expect(pendingDelivery()).toBeNull();
});

test('uncertain submission survives reopening and retries the identical body; another account cannot recover it', async () => {
  mocked.receiveDelivery.mockRejectedValueOnce(new Error('lost connection')).mockResolvedValueOnce(result);
  const view = show();
  selectFirst();
  await reviewAndPost();
  await screen.findByRole('button', { name: 'Retry same delivery' });
  const original = mocked.receiveDelivery.mock.calls[0][0];
  expect(pendingDelivery()?.body).toEqual(original);
  view.unmount();
  show();
  fireEvent.click(screen.getByRole('button', { name: 'Retry same delivery' }));
  await screen.findByText('Delivery received: 2 receipt records.');
  expect(mocked.receiveDelivery.mock.calls[1][0]).toEqual(original);
  sessionStorage.setItem('user', JSON.stringify({ id: 2, company_id: 3 }));
  expect(pendingDelivery()).toBeNull();
});

test('removing a selected line clears its certificate choice when selected again', async () => {
  mocked.uploadReceivingCertificate.mockResolvedValue({ id: 99, file_name: 'heat.pdf', document_number: 'DOC-99' });
  show();
  const one = selectFirst();
  fireEvent.change(one.getByLabelText(/Certificate file/), { target: { files: [new File(['%PDF'], 'heat.pdf')] } });
  await one.findByText('Stored: heat.pdf');
  fireEvent.click(one.getByLabelText(/Receive this line/));
  fireEvent.click(one.getByLabelText(/Receive this line/));
  await waitFor(() => expect(one.queryByText('Stored: heat.pdf')).not.toBeInTheDocument());
});
