/**
 * Change ownership — the reasoned, audited re-owning of one stock row (AS9100D 8.5.3).
 *
 * The server may refuse (409 an empty / inactive row or a no-op, 404 an owner outside the
 * company, 422 a kind/owner mismatch, 503 when the audit row cannot be written), so the
 * dialog is NON-optimistic: nothing is reported as changed until the server answers, a
 * refusal is shown verbatim with the dialog left open and every entry intact, and the
 * dialog cannot be dismissed while the request is in flight. A success hands the server's
 * response — the updated row and its two disclosures — to the host, which turns it into
 * the toast (`ownershipChangeNotice`, covered in utils/ownership.test.ts).
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import ChangeOwnershipDialog, { type OwnershipStockRow } from './ChangeOwnershipDialog';
import { useOwnerDirectory } from '../../hooks/useOwnerDirectory';
import { selectComboBoxOption } from '../../test-utils/comboBox';
import type { InventoryOwnershipChangeResult } from '../../types/ownership';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getCustomerNames: jest.fn(), getVendors: jest.fn(), changeInventoryOwnership: jest.fn() },
}));
const mockedApi = jest.mocked(api);

const companyRow: OwnershipStockRow = {
  id: 31,
  part: { part_number: 'OWN-P-31', name: 'Invented bar stock' },
  location: 'RAW-A',
  warehouse: 'MAIN',
  lot_number: 'HEAT-31',
  quantity_on_hand: 8,
  ownership: 'company',
  owner_customer_id: null,
  owner_vendor_id: null,
  owner_name: null,
};
/** The real response shape: the updated row (timestamps with `Z`) plus both disclosures. */
const changeResult = (overrides: Partial<InventoryOwnershipChangeResult> = {}): InventoryOwnershipChangeResult => ({
  id: 31,
  part_id: 7,
  location: 'RAW-A',
  warehouse: 'MAIN',
  quantity_on_hand: 8,
  quantity_allocated: 0,
  quantity_available: 8,
  lot_number: 'HEAT-31',
  serial_number: null,
  status: 'available',
  is_active: true,
  received_date: '2026-11-02T15:04:05Z',
  expiration_date: null,
  created_at: '2026-11-02T15:04:05Z',
  updated_at: '2026-11-19T16:00:00Z',
  ownership: 'customer',
  owner_customer_id: 5,
  owner_vendor_id: null,
  owner_name: 'Invented Rotor Labs',
  open_pinned_ties: [],
  unpinned_demand: null,
  ...overrides,
});
const customerRow: OwnershipStockRow = {
  ...companyRow,
  id: 32,
  ownership: 'customer',
  owner_customer_id: 4,
  owner_name: 'Invented Aero Works',
};

function Dialog({
  item,
  onClose = jest.fn(),
  onChanged = jest.fn(),
}: {
  item: OwnershipStockRow;
  onClose?: () => void;
  onChanged?: (row: OwnershipStockRow, result: InventoryOwnershipChangeResult) => void;
}) {
  const directory = useOwnerDirectory();
  return <ChangeOwnershipDialog item={item} directory={directory} onClose={onClose} onChanged={onChanged} />;
}

const dialog = () => screen.getByRole('dialog', { name: 'Change ownership' });
const submit = () => within(dialog()).getByRole('button', { name: 'Change ownership' });
const reason = () => within(dialog()).getByRole('textbox', { name: /^Reason/ });
const kind = () => within(dialog()).getByRole('combobox', { name: 'Ownership' });

async function chooseCustomer(name: string) {
  fireEvent.change(kind(), { target: { value: 'customer' } });
  const picker = await within(dialog()).findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(picker).toBeEnabled());
  selectComboBoxOption(picker, name);
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getCustomerNames.mockResolvedValue([
    { id: 4, name: 'Invented Aero Works' },
    { id: 5, name: 'Invented Rotor Labs' },
  ]);
  mockedApi.getVendors.mockResolvedValue([{ id: 8, name: 'Invented Plate Supply', code: 'IPS', is_active: true }]);
  mockedApi.changeInventoryOwnership.mockResolvedValue(changeResult());
});

/** Render, then let the owner list its open picker asked for arrive (no stray act warnings). */
async function renderSettled(item: OwnershipStockRow, props: { onClose?: () => void; onChanged?: () => void } = {}) {
  const view = render(<Dialog item={item} {...props} />);
  await act(async () => undefined);
  return view;
}

it('describes the row and its current owner, and loads only the list its open picker shows', async () => {
  await renderSettled(customerRow);

  expect(dialog()).toHaveTextContent('OWN-P-31');
  expect(dialog()).toHaveTextContent('MAIN / RAW-A · Lot HEAT-31');
  expect(dialog()).toHaveTextContent('On hand 8');
  expect(within(dialog()).getAllByText('Customer: Invented Aero Works').length).toBeGreaterThan(0);
  expect(dialog()).toHaveTextContent('No quantity moves and no ledger row is posted');
  expect(mockedApi.getCustomerNames).toHaveBeenCalledTimes(1);
  expect(mockedApi.getVendors).not.toHaveBeenCalled();
});

it('cannot submit until a DIFFERENT owner and a reason are given', async () => {
  render(<Dialog item={companyRow} />);

  expect(submit()).toBeDisabled();
  expect(dialog()).toHaveTextContent('Choose a different owner to change this row.');
  expect(mockedApi.getCustomerNames).not.toHaveBeenCalled();

  fireEvent.change(kind(), { target: { value: 'customer' } });
  fireEvent.change(reason(), { target: { value: 'Customer-furnished bar' } });
  expect(submit()).toBeDisabled(); // no customer chosen yet
  await chooseCustomer('Invented Rotor Labs');
  expect(submit()).toBeEnabled();
  fireEvent.change(reason(), { target: { value: '   ' } });
  expect(submit()).toBeDisabled();
  expect(mockedApi.changeInventoryOwnership).not.toHaveBeenCalled();
});

it('posts the whole triple with the trimmed reason and reports success only after the server answers', async () => {
  let resolve: (value: unknown) => void = () => undefined;
  mockedApi.changeInventoryOwnership.mockReturnValue(new Promise(r => (resolve = r)) as never);
  const onChanged = jest.fn();
  const onClose = jest.fn();
  render(<Dialog item={companyRow} onChanged={onChanged} onClose={onClose} />);

  await chooseCustomer('Invented Rotor Labs');
  fireEvent.change(reason(), { target: { value: '  Customer-furnished bar  ' } });
  fireEvent.click(submit());

  expect(mockedApi.changeInventoryOwnership).toHaveBeenCalledWith(31, {
    ownership: 'customer',
    owner_customer_id: 5,
    owner_vendor_id: null,
    reason: 'Customer-furnished bar',
  });
  // In flight: nothing reported, and the dialog cannot be dismissed.
  expect(onChanged).not.toHaveBeenCalled();
  expect(within(dialog()).getByRole('button', { name: 'Cancel' })).toBeDisabled();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(onClose).not.toHaveBeenCalled();

  const result = changeResult({
    open_pinned_ties: [
      { allocation_id: 3, work_order_id: 40, work_order_number: 'WO-INV-40', outstanding_quantity: 2 },
    ],
    unpinned_demand: {
      open_unpinned_demand: 6,
      company_stock_remaining: 2,
      short_by: 4,
      work_order_numbers: ['WO-INV-41'],
    },
  });
  await act(async () => resolve(result));
  // The response — disclosures included — reaches the host untouched, so it can warn.
  expect(onChanged).toHaveBeenCalledWith(companyRow, result);
});

it('re-owns customer stock to the company without sending a stale customer id', async () => {
  await renderSettled(customerRow);

  fireEvent.change(kind(), { target: { value: 'company' } });
  fireEvent.change(reason(), { target: { value: 'Bought outright' } });
  fireEvent.click(submit());

  await waitFor(() =>
    expect(mockedApi.changeInventoryOwnership).toHaveBeenCalledWith(32, {
      ownership: 'company',
      owner_customer_id: null,
      owner_vendor_id: null,
      reason: 'Bought outright',
    })
  );
});

it('re-owns to a supplier with only the vendor id', async () => {
  await renderSettled(customerRow);

  fireEvent.change(kind(), { target: { value: 'supplier' } });
  const supplier = await within(dialog()).findByRole('combobox', { name: /^Owning supplier/ });
  await waitFor(() => expect(within(supplier).getAllByRole('option')).toHaveLength(2));
  fireEvent.change(supplier, { target: { value: '8' } });
  fireEvent.change(reason(), { target: { value: 'Consigned by the supplier' } });
  fireEvent.click(submit());

  await waitFor(() =>
    expect(mockedApi.changeInventoryOwnership).toHaveBeenCalledWith(32, {
      ownership: 'supplier',
      owner_customer_id: null,
      owner_vendor_id: 8,
      reason: 'Consigned by the supplier',
    })
  );
  expect(mockedApi.getVendors).toHaveBeenCalledWith({ active_only: false });
});

it('shows a refusal verbatim and keeps the dialog open with every entry intact', async () => {
  mockedApi.changeInventoryOwnership.mockRejectedValue({
    response: {
      status: 409,
      data: {
        detail:
          'Inventory item 31 has no stock on hand (0); ownership is recorded only for material that is physically there.',
      },
    },
  });
  const onChanged = jest.fn();
  const onClose = jest.fn();
  render(<Dialog item={companyRow} onChanged={onChanged} onClose={onClose} />);

  await chooseCustomer('Invented Aero Works');
  fireEvent.change(reason(), { target: { value: 'Customer-furnished bar' } });
  fireEvent.click(submit());

  expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
    'Inventory item 31 has no stock on hand (0); ownership is recorded only for material that is physically there.'
  );
  expect(onChanged).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(reason()).toHaveValue('Customer-furnished bar');
  expect(kind()).toHaveValue('customer');
  expect(within(dialog()).getByRole('combobox', { name: /^Owning customer/ })).toHaveValue('Invented Aero Works');
  expect(submit()).toBeEnabled();
});

it('Cancel closes without calling the server', () => {
  const onClose = jest.fn();
  render(<Dialog item={companyRow} onClose={onClose} />);

  fireEvent.click(within(dialog()).getByRole('button', { name: 'Cancel' }));

  expect(onClose).toHaveBeenCalledTimes(1);
  expect(mockedApi.changeInventoryOwnership).not.toHaveBeenCalled();
});
