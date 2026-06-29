/**
 * Customers — Batch 6 form-level locks (FormField label association +
 * useUnsavedChanges discard guard).
 *
 * These are the behavioral locks for the Batch 6 forms pass, exercised through
 * the real Customers create/edit modal (not the primitive in isolation):
 *
 *   1. LABEL ASSOCIATION WORKS. Every visible field is reachable via
 *      getByLabelText — i.e. each <label> is now tied to its control through the
 *      FormField-generated htmlFor/id. Before Batch 6 these labels carried no
 *      `for`, so getByLabelText could not resolve them; this proves the
 *      537-labels-vs-11-htmlFor gap is closed on this form.
 *
 *   2. UNSAVED-CHANGES GUARD. Editing a field then trying to Cancel/close the
 *      modal routes through useUnsavedChanges.confirmDiscard(), which gates on
 *      window.confirm:
 *        - dirty + confirm cancelled  -> modal stays open, nothing discarded,
 *        - dirty + confirm accepted   -> modal closes,
 *        - clean (untouched) form     -> closes with NO confirm prompt,
 *        - successful SAVE            -> closes with NO confirm prompt (the save
 *          path calls setShowModal(false) directly, never confirmDiscard). This
 *          last one is the key lock: a save must never nag the user.
 *
 * The modal portals to document.body (shared <Modal>), so we query inputs via
 * accessible name and reach the form for submit through the name input.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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

/** Open the Add Customer modal and return its <form>. */
async function openCreateModal() {
  fireEvent.click(await screen.findByRole('button', { name: /add customer/i }));
  // The Customer Name field only exists inside the open modal.
  const nameInput = await screen.findByLabelText(/Customer Name/);
  return { nameInput, form: nameInput.closest('form') as HTMLFormElement };
}

describe('Customers form — label association (FormField)', () => {
  let confirmSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getCustomers.mockResolvedValue([]);
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    confirmSpy.mockRestore();
  });

  it('exposes every modal field via its label (htmlFor/id wired by FormField)', async () => {
    renderCustomers();
    const { form } = await openCreateModal();
    const scoped = within(form);

    // Each of these resolves ONLY because the <label for> points at the control
    // id FormField generated. Pre-Batch-6 these labels had no `for`.
    expect(scoped.getByLabelText(/Customer Name/)).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Contact Name')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Email')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Phone')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Address Line 1')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Address Line 2')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('City')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('State')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('ZIP')).toBeInstanceOf(HTMLInputElement);
    // A <select> field proves FormField wiring works for non-input controls too.
    expect(scoped.getByLabelText('Payment Terms')).toBeInstanceOf(HTMLSelectElement);
    expect(scoped.getByLabelText('Special Requirements')).toBeInstanceOf(HTMLTextAreaElement);
    expect(scoped.getByLabelText('Notes')).toBeInstanceOf(HTMLTextAreaElement);
  });

  it('marks the required Customer Name field with aria-required', async () => {
    renderCustomers();
    const { nameInput } = await openCreateModal();
    expect(nameInput).toHaveAttribute('aria-required', 'true');
    // A non-required field carries no aria-required.
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-required');
  });
});

describe('Customers form — unsaved-changes discard guard', () => {
  let confirmSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getCustomers.mockResolvedValue([]);
  });

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing a clean (untouched) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    renderCustomers();
    await openCreateModal();

    // Cancel an untouched form — confirmDiscard short-circuits true (clean), so
    // window.confirm is never shown and the modal closes.
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByLabelText(/Customer Name/)).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user cancels the discard confirm', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false); // user clicks "Cancel" in the dialog
    renderCustomers();
    const { nameInput } = await openCreateModal();

    // Dirty the form.
    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });

    // Attempt to close via Cancel — the guard fires, the user declines, modal stays.
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    // Modal is still open and the edit is preserved.
    expect(screen.getByLabelText(/Customer Name/)).toHaveValue('Acme Aerospace');
  });

  it('prompts and closes when the user confirms the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true); // user accepts the discard
    renderCustomers();
    const { nameInput } = await openCreateModal();

    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByLabelText(/Customer Name/)).not.toBeInTheDocument()
    );
    // Nothing was persisted — discarding must not write.
    expect(mockedApi.createCustomer).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful save even though the form is dirty', async () => {
    // This is the key behavioral lock: the save path must close the modal
    // directly, never routing through confirmDiscard, so the user is never
    // nagged after a successful save.
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockedApi.createCustomer.mockResolvedValue({ id: 1 });
    renderCustomers();
    const { nameInput, form } = await openCreateModal();

    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    // Submit the form (the dirty state is what would trigger a prompt if the
    // save path wrongly went through the guard).
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createCustomer).toHaveBeenCalledTimes(1));
    // Modal closed by the save path...
    await waitFor(() =>
      expect(screen.queryByLabelText(/Customer Name/)).not.toBeInTheDocument()
    );
    // ...and crucially, no discard prompt was ever shown.
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
