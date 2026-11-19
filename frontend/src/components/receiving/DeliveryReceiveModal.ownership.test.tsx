/**
 * Multi-line delivery receipt — who owns each line's material (AS9100D 8.5.3).
 *
 * Every line carries its own ownership: one delivery can bring the shop's own stock and a
 * customer's furnished material together. The owner lists load once for the whole modal,
 * the review step names each line's owner, and the posted body carries the exact triple the
 * server's CHECK wants on every line.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DeliveryReceiveModal from './DeliveryReceiveModal';
import api from '../../services/api';
import { selectComboBoxOption } from '../../test-utils/comboBox';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    receiveDelivery: jest.fn(),
    uploadReceivingCertificate: jest.fn(),
    downloadDocument: jest.fn(),
    getDocument: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
  },
}));
jest.mock('../../hooks/useUnsavedChanges', () => ({
  __esModule: true,
  default: () => ({ confirmDiscard: () => true, markSaved: jest.fn() }),
}));
const mocked = jest.mocked(api);

const po = {
  po_id: 10,
  po_number: 'PO-10',
  vendor_id: 8,
  vendor_name: 'Invented Plate Supply',
  lines: [
    { line_id: 11, part_number: 'P-ONE', part_name: 'First part', quantity_remaining: 10 },
    { line_id: 12, part_number: 'P-TWO', part_name: 'Second part', quantity_remaining: 4 },
  ],
};
const result = {
  batch_id: 1,
  idempotency_key: 'owned-delivery',
  receipts: [
    { id: 1, receipt_number: 'RCV-ONE', quantity_received: 10, lot_number: 'LOT-ONE' },
    { id: 2, receipt_number: 'RCV-TWO', quantity_received: 4, lot_number: 'LOT-TWO' },
  ],
};

const show = () => render(<DeliveryReceiveModal po={po} locations={[]} onClose={jest.fn()} onSaved={jest.fn()} />);
const lineGroup = (name: string) => within(screen.getByRole('group', { name }));
function selectLine(name: string) {
  const line = lineGroup(name);
  fireEvent.click(line.getByLabelText(/Receive this line/));
  return line;
}

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 2 }));
  mocked.getCustomerNames.mockResolvedValue([{ id: 4, name: 'Invented Aero Works' }]);
  mocked.getVendors.mockResolvedValue([
    { id: 8, name: 'Invented Plate Supply', code: 'IPS', is_active: true },
    { id: 9, name: 'Invented Consignment Metals', code: 'ICM', is_active: true },
  ]);
  mocked.receiveDelivery.mockResolvedValue(result);
});

it('posts each line with its own ownership and names the owner in the review', async () => {
  show();
  const one = selectLine('P-ONE · First part');
  const two = selectLine('P-TWO · Second part');
  expect(one.getByRole('combobox', { name: 'Ownership' })).toHaveValue('company');

  fireEvent.change(two.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  const customer = await two.findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(customer).toBeEnabled());
  selectComboBoxOption(customer, 'Invented Aero Works');
  fireEvent.click(screen.getByRole('button', { name: 'Review delivery' }));

  const review = screen.getAllByRole('listitem');
  expect(review[0]).toHaveTextContent('P-ONE');
  expect(review[0]).toHaveTextContent('Company-owned');
  expect(review[1]).toHaveTextContent('P-TWO');
  expect(review[1]).toHaveTextContent('Customer-owned · Invented Aero Works');

  fireEvent.click(screen.getByRole('button', { name: /Receive \d lines/ }));
  await screen.findByText('Delivery received: 2 receipt records.');
  expect(mocked.receiveDelivery.mock.calls[0][0].lines).toEqual([
    expect.objectContaining({ po_line_id: 11, ownership: 'company', owner_customer_id: null, owner_vendor_id: null }),
    expect.objectContaining({ po_line_id: 12, ownership: 'customer', owner_customer_id: 4, owner_vendor_id: null }),
  ]);
  expect(mocked.getVendors).not.toHaveBeenCalled();
});

it('pre-selects the PO’s own vendor for a supplier-owned (consigned) line, still editable', async () => {
  show();
  const one = selectLine('P-ONE · First part');

  fireEvent.change(one.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'supplier' } });
  const supplier = await one.findByRole('combobox', { name: /^Owning supplier/ });
  await waitFor(() => expect(within(supplier).getAllByRole('option')).toHaveLength(3));
  expect(supplier).toHaveValue('8');
  fireEvent.click(screen.getByRole('button', { name: 'Review delivery' }));
  expect(screen.getByRole('listitem')).toHaveTextContent('Supplier-owned · Invented Plate Supply');

  fireEvent.click(screen.getByRole('button', { name: /Receive \d line/ }));
  await waitFor(() => expect(mocked.receiveDelivery).toHaveBeenCalledTimes(1));
  expect(mocked.receiveDelivery.mock.calls[0][0].lines).toEqual([
    expect.objectContaining({ po_line_id: 11, ownership: 'supplier', owner_customer_id: null, owner_vendor_id: 8 }),
  ]);
});

it('refuses to review a line marked customer-owned without its customer', async () => {
  show();
  const one = selectLine('P-ONE · First part');

  fireEvent.change(one.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  fireEvent.click(screen.getByRole('button', { name: 'Review delivery' }));

  expect(
    await screen.findByText('Choose the owning customer or supplier for each line that is not company-owned.')
  ).toBeInTheDocument();
  expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  expect(mocked.receiveDelivery).not.toHaveBeenCalled();
});

it('loads the customer list once for every line that needs it', async () => {
  show();
  const one = selectLine('P-ONE · First part');
  const two = selectLine('P-TWO · Second part');

  fireEvent.change(one.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  fireEvent.change(two.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  await waitFor(() => expect(two.getByRole('combobox', { name: /^Owning customer/ })).toBeEnabled());

  expect(mocked.getCustomerNames).toHaveBeenCalledTimes(1);
});

it('posts an owned line held for inspection with its owner, so the owner reaches stock on acceptance', async () => {
  show();
  const one = selectLine('P-ONE · First part');
  fireEvent.click(one.getByLabelText('Requires inspection'));
  fireEvent.change(one.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  const customer = await one.findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(customer).toBeEnabled());
  selectComboBoxOption(customer, 'Invented Aero Works');
  fireEvent.click(screen.getByRole('button', { name: 'Review delivery' }));

  expect(screen.getByRole('listitem')).toHaveTextContent('Hold for inspection');
  expect(screen.getByRole('listitem')).toHaveTextContent('Customer-owned · Invented Aero Works');
  fireEvent.click(screen.getByRole('button', { name: /Receive \d line/ }));
  await waitFor(() => expect(mocked.receiveDelivery).toHaveBeenCalledTimes(1));
  expect(mocked.receiveDelivery.mock.calls[0][0].lines).toEqual([
    expect.objectContaining({
      po_line_id: 11,
      requires_inspection: true,
      ownership: 'customer',
      owner_customer_id: 4,
      owner_vendor_id: null,
    }),
  ]);
});

it('shows the server’s refusal of an owner verbatim and keeps the entries', async () => {
  mocked.receiveDelivery.mockRejectedValue({
    response: {
      status: 404,
      data: { detail: 'Delivery line 1: Owning customer not found. No delivery lines were posted.' },
    },
  });
  show();
  const one = selectLine('P-ONE · First part');
  fireEvent.change(one.getByRole('combobox', { name: 'Ownership' }), { target: { value: 'customer' } });
  const customer = await one.findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(customer).toBeEnabled());
  selectComboBoxOption(customer, 'Invented Aero Works');
  fireEvent.click(screen.getByRole('button', { name: 'Review delivery' }));
  fireEvent.click(screen.getByRole('button', { name: /Receive \d line/ }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Delivery line 1: Owning customer not found. No delivery lines were posted.'
  );
  const line = lineGroup('P-ONE · First part');
  expect(line.getByRole('combobox', { name: 'Ownership' })).toHaveValue('customer');
  expect(line.getByRole('combobox', { name: /^Owning customer/ })).toHaveValue('Invented Aero Works');
  expect(screen.getByRole('button', { name: 'Review delivery' })).toBeEnabled();
});
