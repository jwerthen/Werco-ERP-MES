import React, { useState } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import OwnershipFields from './OwnershipFields';
import { useOwnerDirectory } from '../../hooks/useOwnerDirectory';
import type { OwnershipSelection } from '../../types/ownership';
import { COMPANY_OWNERSHIP } from '../../utils/ownership';
import { comboBoxOptions, openComboBox, selectComboBoxOption } from '../../test-utils/comboBox';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getCustomerNames: jest.fn(), getVendors: jest.fn() },
}));
const mockedApi = jest.mocked(api);

const customers = [
  { id: 4, name: 'Invented Aero Works' },
  { id: 5, name: 'Invented Rotor Labs' },
];
const vendors = [
  { id: 8, name: 'Invented Plate Supply', code: 'IPS', is_active: true },
  { id: 9, name: 'Invented Consignment Metals', code: 'ICM', is_active: false },
];

/** A screen's worth of state: one directory, one selection, every change recorded. */
function Harness({
  initial = COMPANY_OWNERSHIP,
  defaultVendorId = null,
  currentOwnerName = null,
  onChange = () => undefined,
}: {
  initial?: OwnershipSelection;
  defaultVendorId?: number | null;
  currentOwnerName?: string | null;
  onChange?: (next: OwnershipSelection) => void;
}) {
  const directory = useOwnerDirectory();
  const [value, setValue] = useState<OwnershipSelection>(initial);
  return (
    <OwnershipFields
      value={value}
      onChange={next => {
        onChange(next);
        setValue(next);
      }}
      directory={directory}
      defaultVendorId={defaultVendorId}
      currentOwnerName={currentOwnerName}
    />
  );
}

const kindSelect = () => screen.getByRole('combobox', { name: 'Ownership' });

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getCustomerNames.mockResolvedValue(customers);
  mockedApi.getVendors.mockResolvedValue(vendors);
});

it('starts company-owned with no owner picker and fetches no owner list', () => {
  render(<Harness />);

  expect(kindSelect()).toHaveValue('company');
  expect(
    within(kindSelect())
      .getAllByRole('option')
      .map(option => option.textContent)
  ).toEqual(['Company-owned', 'Customer-owned', 'Supplier-owned']);
  expect(screen.queryByRole('combobox', { name: /^Owning customer/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: /^Owning supplier/ })).not.toBeInTheDocument();
  expect(mockedApi.getCustomerNames).not.toHaveBeenCalled();
  expect(mockedApi.getVendors).not.toHaveBeenCalled();
});

it('loads customers lazily, once, and reports the chosen customer', async () => {
  const onChange = jest.fn();
  render(<Harness onChange={onChange} />);

  fireEvent.change(kindSelect(), { target: { value: 'customer' } });
  const picker = await screen.findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(picker).toBeEnabled());
  expect(screen.getByText(/automatic issue and backflush skip it/)).toBeInTheDocument();
  openComboBox(picker);
  expect(comboBoxOptions(picker).map(option => option.textContent)).toEqual([
    'Invented Aero Works',
    'Invented Rotor Labs',
  ]);
  fireEvent.keyDown(picker, { key: 'Escape' });
  selectComboBoxOption(picker, 'Invented Rotor Labs');

  expect(onChange).toHaveBeenLastCalledWith({ ownership: 'customer', owner_customer_id: 5, owner_vendor_id: null });
  fireEvent.change(kindSelect(), { target: { value: 'company' } });
  fireEvent.change(kindSelect(), { target: { value: 'customer' } });
  await screen.findByRole('combobox', { name: /^Owning customer/ });
  expect(mockedApi.getCustomerNames).toHaveBeenCalledTimes(1);
  expect(mockedApi.getVendors).not.toHaveBeenCalled();
});

it('lists every non-deleted vendor (inactive ones labelled) and pre-selects the default vendor', async () => {
  const onChange = jest.fn();
  render(<Harness defaultVendorId={8} onChange={onChange} />);

  fireEvent.change(kindSelect(), { target: { value: 'supplier' } });

  expect(onChange).toHaveBeenLastCalledWith({ ownership: 'supplier', owner_customer_id: null, owner_vendor_id: 8 });
  const picker = await screen.findByRole('combobox', { name: /^Owning supplier/ });
  await waitFor(() => expect(within(picker).getAllByRole('option')).toHaveLength(3));
  expect(mockedApi.getVendors).toHaveBeenCalledWith({ active_only: false });
  expect(
    within(picker)
      .getAllByRole('option')
      .map(option => option.textContent)
  ).toEqual(['Select a supplier…', 'Invented Plate Supply (IPS)', 'Invented Consignment Metals (ICM) — inactive']);
  expect(picker).toHaveValue('8');
  fireEvent.change(picker, { target: { value: '9' } });
  expect(onChange).toHaveBeenLastCalledWith({ ownership: 'supplier', owner_customer_id: null, owner_vendor_id: 9 });
});

it('switching kind clears the owner id that does not belong to the new kind', async () => {
  const onChange = jest.fn();
  render(
    <Harness initial={{ ownership: 'customer', owner_customer_id: 4, owner_vendor_id: null }} onChange={onChange} />
  );
  await screen.findByRole('combobox', { name: /^Owning customer/ });
  await act(async () => undefined); // let the customer list the open picker asked for arrive

  fireEvent.change(kindSelect(), { target: { value: 'supplier' } });
  expect(onChange).toHaveBeenLastCalledWith({ ownership: 'supplier', owner_customer_id: null, owner_vendor_id: null });

  fireEvent.change(kindSelect(), { target: { value: 'company' } });
  expect(onChange).toHaveBeenLastCalledWith({ ownership: 'company', owner_customer_id: null, owner_vendor_id: null });
  await act(async () => undefined); // and the supplier list the brief switch asked for
});

it('keeps showing a current owner the list no longer offers, by its recorded name', async () => {
  render(
    <Harness
      initial={{ ownership: 'customer', owner_customer_id: 77, owner_vendor_id: null }}
      currentOwnerName="Invented Former Customer"
    />
  );

  const picker = await screen.findByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(mockedApi.getCustomerNames).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(picker).toHaveValue('Invented Former Customer'));
  openComboBox(picker);
  expect(comboBoxOptions(picker).map(option => option.textContent)).toEqual([
    'Invented Former Customer',
    'Invented Aero Works',
    'Invented Rotor Labs',
  ]);
});

it('shows a load failure in the field and retries on request', async () => {
  mockedApi.getCustomerNames.mockRejectedValueOnce(new Error('offline'));
  render(<Harness />);

  fireEvent.change(kindSelect(), { target: { value: 'customer' } });

  expect(await screen.findByText('Could not load customers.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(mockedApi.getCustomerNames).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText('Could not load customers.')).not.toBeInTheDocument());
  const picker = screen.getByRole('combobox', { name: /^Owning customer/ });
  await waitFor(() => expect(picker).toBeEnabled());
  openComboBox(picker);
  expect(comboBoxOptions(picker)).toHaveLength(2);
});
