import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import CustomerPOAutocomplete from './CustomerPOAutocomplete';
import api from '../../services/api';
import type { CustomerPOSuggestionsResponse } from '../../types/customerPurchaseOrders';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getCustomerPOSuggestions: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;
const selected = jest.fn();
const submit = jest.fn();
const suggestions: CustomerPOSuggestionsResponse = {
  items: [
    { customer_name: 'Acme & Co', customer_po: 'PO/100 #A' },
    { customer_name: 'Beta Aero', customer_po: 'PO/100 #A' },
  ],
  has_more: false,
};

function Harness({ customerName }: { customerName?: string }) {
  const [value, setValue] = useState('');
  return (
    <form
      onSubmit={event => {
        event.preventDefault();
        submit(value);
      }}
    >
      <label htmlFor="customer-po">Customer PO</label>
      <CustomerPOAutocomplete
        id="customer-po"
        value={value}
        customerName={customerName}
        onChange={setValue}
        onSelect={selected}
      />
      <button type="submit">Save</button>
    </form>
  );
}
async function type(value: string) {
  fireEvent.change(screen.getByRole('combobox', { name: 'Customer PO' }), { target: { value } });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  sessionStorage.setItem('token', 'company-one');
  mockedApi.getCustomerPOSuggestions.mockResolvedValue(suggestions);
});
afterEach(() => {
  jest.useRealTimers();
  sessionStorage.clear();
});

it('starts matching on the first character, debounces, and does not replace typed text', async () => {
  render(<Harness customerName="Acme & Co" />);
  const input = screen.getByRole('combobox', { name: 'Customer PO' });
  fireEvent.focus(input);
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(mockedApi.getCustomerPOSuggestions).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: 'P' } });
  expect(mockedApi.getCustomerPOSuggestions).not.toHaveBeenCalled();
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(mockedApi.getCustomerPOSuggestions).toHaveBeenCalledWith(
    { search: 'P', customer_name: 'Acme & Co', limit: 10 },
    expect.any(AbortSignal)
  );
  expect(input).toHaveValue('P');
  expect(input).toHaveAttribute('aria-expanded', 'true');
});

it('disambiguates identical PO numbers by customer and chooses using the keyboard without submitting', async () => {
  render(<Harness />);
  await type('PO/1');
  const input = screen.getByRole('combobox', { name: 'Customer PO' });
  expect(mockedApi.getCustomerPOSuggestions).toHaveBeenCalledWith(
    { search: 'PO/1', limit: 10 },
    expect.any(AbortSignal)
  );
  expect(screen.getAllByRole('option')).toHaveLength(2);
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  expect(screen.getByRole('option', { name: /Beta Aero/ })).toHaveAttribute('aria-selected', 'true');
  expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(false);
  expect(selected).toHaveBeenCalledWith(suggestions.items[1]);
  expect(input).toHaveValue('PO/100 #A');
  expect(input).toHaveAttribute('aria-expanded', 'false');
  expect(submit).not.toHaveBeenCalled();
});

it('selects an exact PO by pointer and Escape keeps free text unchanged', async () => {
  render(<Harness />);
  await type('PO');
  fireEvent.click(screen.getByRole('option', { name: /Acme & Co/ }));
  expect(selected).toHaveBeenCalledWith(suggestions.items[0]);
  await type('NEW-PO');
  fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' });
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(screen.getByRole('combobox')).toHaveValue('NEW-PO');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  expect(submit).toHaveBeenCalledWith('NEW-PO');
});

it('keeps keyboard-selected options visible when moving through a full suggestion list', async () => {
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({
    items: Array.from({ length: 10 }, (_, index) => ({ customer_name: 'Acme', customer_po: `PO-${index}` })),
    has_more: true,
  });
  render(<Harness />);
  await type('PO');
  const options = screen.getAllByRole('option');
  options.forEach(option => {
    option.scrollIntoView = jest.fn();
  });
  const input = screen.getByRole('combobox');
  for (let index = 0; index < 10; index += 1) fireEvent.keyDown(input, { key: 'ArrowDown' });
  expect(options[9]).toHaveAttribute('aria-selected', 'true');
  expect(options[9].scrollIntoView).toHaveBeenLastCalledWith({ block: 'nearest' });
});

it('shows an empty result and still accepts a new PO', async () => {
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({ items: [], has_more: false });
  render(<Harness />);
  await type('NEW-100');
  expect(screen.getByRole('status')).toHaveTextContent('You can use this as a new PO number');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  expect(submit).toHaveBeenCalledWith('NEW-100');
});

it('keeps entry usable if suggestions fail', async () => {
  mockedApi.getCustomerPOSuggestions.mockRejectedValue(new Error('Offline'));
  render(<Harness />);
  await type('PO-200');
  expect(screen.getByRole('status')).toHaveTextContent('Suggestions unavailable');
  expect(screen.getByRole('combobox')).toHaveValue('PO-200');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  expect(submit).toHaveBeenCalledWith('PO-200');
});

it('clears old customer matches immediately and ignores their pending response', async () => {
  let resolveOld!: (value: CustomerPOSuggestionsResponse) => void;
  mockedApi.getCustomerPOSuggestions.mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve;
    })
  );
  const { rerender } = render(<Harness customerName="Acme & Co" />);
  await type('PO');
  rerender(<Harness customerName="Beta Aero" />);
  expect(screen.queryByRole('option')).not.toBeInTheDocument();
  await act(async () => resolveOld(suggestions));
  expect(screen.queryByRole('option')).not.toBeInTheDocument();
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({ items: [suggestions.items[1]], has_more: false });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(screen.getByRole('option')).toHaveTextContent('Beta Aero');
  expect(mockedApi.getCustomerPOSuggestions).toHaveBeenLastCalledWith(
    { search: 'PO', customer_name: 'Beta Aero', limit: 10 },
    expect.any(AbortSignal)
  );
});

it('ignores stale prefix responses and narrows the suggestion limit', async () => {
  let resolveOld!: (value: CustomerPOSuggestionsResponse) => void;
  mockedApi.getCustomerPOSuggestions.mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve;
    })
  );
  render(<Harness />);
  await type('P');
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({ items: [suggestions.items[1]], has_more: true });
  await type('PO/10');
  await act(async () => resolveOld(suggestions));
  expect(screen.getAllByRole('option')).toHaveLength(1);
  expect(screen.getByRole('option')).toHaveTextContent('Beta Aero');
  expect(screen.getByText('Keep typing to narrow the matches.')).toBeInTheDocument();
});

it('discards old-company suggestions when only the platform access token changes', async () => {
  render(<Harness />);
  await type('PO');
  expect(screen.getAllByRole('option')).toHaveLength(2);
  let resolveOld!: (value: CustomerPOSuggestionsResponse) => void;
  mockedApi.getCustomerPOSuggestions.mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve;
    })
  );
  await type('PO/');
  act(() => {
    sessionStorage.setItem('token', 'company-two');
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  expect(screen.queryByRole('option')).not.toBeInTheDocument();
  await act(async () => resolveOld(suggestions));
  expect(screen.queryByRole('option')).not.toBeInTheDocument();
  mockedApi.getCustomerPOSuggestions.mockResolvedValue({
    items: [{ customer_name: 'Other tenant', customer_po: 'PO/999' }],
    has_more: false,
  });
  await act(async () => {
    jest.advanceTimersByTime(200);
  });
  expect(screen.getByRole('option')).toHaveTextContent('PO/999');
});
