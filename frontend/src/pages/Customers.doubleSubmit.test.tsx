/**
 * Customers — create double-submit guard.
 *
 * Batch 1: the create/update submit uses LoadingButton + a `saving` flag and a
 * re-entrancy short-circuit (`if (saving) return`). A second submit while the
 * first is in flight must NOT fire a second createCustomer call.
 *
 * We submit the form directly (fireEvent.submit) to simulate rapid re-entry
 * (e.g. double Enter) — that path bypasses the button's disabled attribute, so
 * passing this proves the handler-level guard, not just the disabled button.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Customers from './Customers';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getCustomers: jest.fn(),
    getCustomerStats: jest.fn(),
    createCustomer: jest.fn(),
    updateCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

function renderCustomers() {
  return render(
    <MemoryRouter initialEntries={['/customers']}>
      <Customers />
    </MemoryRouter>
  );
}

describe('Customers create double-submit guard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getCustomers.mockResolvedValue([]);
  });

  it('disables the submit button and fires createCustomer only once on a double submit', async () => {
    // Keep the first create in flight so `saving` stays true across the retry.
    let resolveCreate: (value: unknown) => void = () => undefined;
    mockedApi.createCustomer.mockImplementation(
      () => new Promise((resolve) => { resolveCreate = resolve; })
    );

    renderCustomers();

    // Open the Add Customer modal.
    fireEvent.click(await screen.findByRole('button', { name: /add customer/i }));

    // The modal portals to document.body; the customer name is the only
    // `required` field on the create form.
    const nameInput = document.body.querySelector('form input[required]') as HTMLInputElement;
    expect(nameInput).not.toBeNull();
    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    expect(nameInput).toHaveValue('Acme Aerospace');

    // First submit — kicks off the (hung) create.
    const form = nameInput.closest('form')!;
    fireEvent.submit(form);

    // The submit button is now disabled and shows the loading label.
    const submitBtn = await screen.findByRole('button', { name: /saving/i });
    expect(submitBtn).toBeDisabled();

    // Rapid re-entry while still saving must be a no-op.
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createCustomer).toHaveBeenCalledTimes(1));

    // Let the create settle so the test ends cleanly.
    resolveCreate({ id: 1 });
    await waitFor(() => expect(mockedApi.getCustomers).toHaveBeenCalledTimes(2));
  });
});
