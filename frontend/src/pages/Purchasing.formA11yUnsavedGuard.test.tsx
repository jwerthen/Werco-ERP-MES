/**
 * Purchasing — useUnsavedChanges discard guard on the PO / vendor form modals.
 *
 * Clones the Customers/Materials formA11yUnsavedGuard template for the three
 * Purchasing form modals (Create PO, Create Vendor, Edit Vendor):
 *
 *   - clean (untouched) form  -> Cancel closes with NO confirm prompt,
 *   - dirty + declined        -> modal stays open, the edit is preserved,
 *   - dirty + confirmed       -> modal closes, nothing saved,
 *   - successful SAVE         -> closes directly, NO prompt (the submit paths
 *     call setShowXxxModal(false) directly, never confirmDiscard),
 *   - a beforeunload listener is registered only while the form is dirty.
 *
 * The guard is wired into the Modal onClose (not just the Cancel button), so
 * the header X / Escape paths are covered by the same requestClose handlers.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Purchasing from './Purchasing';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    getDocuments: jest.fn(),
    getDocumentTypes: jest.fn(),
    createPurchaseOrder: jest.fn(),
    createVendor: jest.fn(),
    updateVendor: jest.fn(),
  },
}));

// The page gates its create/send actions by role (useAuth); manager sees all.
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const vendors = [
  {
    id: 1,
    code: 'VND-001',
    name: 'Acme Aerospace',
    contact_name: 'Pat Lee',
    email: 'pat@acme.test',
    phone: '555-0101',
    country: 'US',
    payment_terms: 'NET 30',
    is_approved: true,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    notes: '',
    version: 0,
  },
];

const renderPurchasing = () =>
  render(
    <MemoryRouter initialEntries={['/purchasing']}>
      <Purchasing />
    </MemoryRouter>
  );

/** Load the page and open the Create Vendor modal; returns the Code input + form. */
async function openCreateVendorModal() {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
  fireEvent.click(screen.getByRole('button', { name: /new vendor/i }));
  const codeInput = await screen.findByLabelText(/^Code/);
  return { codeInput, form: codeInput.closest('form') as HTMLFormElement };
}

// One PO row so the orders tab renders the table (not the EmptyState, whose
// "New PO" action would make the header button query ambiguous).
const purchaseOrders = [
  {
    id: 10,
    po_number: 'PO-1001',
    vendor_id: 1,
    vendor_name: 'Acme Aerospace',
    status: 'draft',
    order_date: '2026-06-20',
    required_date: '2026-07-01',
    total: 1250.5,
    line_count: 3,
  },
];

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue(purchaseOrders as any);
  mockedApi.getParts.mockResolvedValue([] as any);
  mockedApi.getDocuments.mockResolvedValue([] as any);
  mockedApi.getDocumentTypes.mockResolvedValue([] as any);
});

describe('Purchasing — Create Vendor unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing a clean (untouched) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openCreateVendorModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Create Vendor' })).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user declines the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { codeInput } = await openCreateVendorModal();

    fireEvent.change(codeInput, { target: { value: 'VND-099' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^Code/)).toHaveValue('VND-099');
  });

  it('prompts and closes (discarding the entry) when the user confirms', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const { codeInput } = await openCreateVendorModal();

    fireEvent.change(codeInput, { target: { value: 'VND-099' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Create Vendor' })).not.toBeInTheDocument()
    );
    expect(mockedApi.createVendor).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful save even though the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockedApi.createVendor.mockResolvedValue({ id: 9 } as any);
    const { codeInput, form } = await openCreateVendorModal();

    fireEvent.change(codeInput, { target: { value: 'VND-099' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'New Vendor Co' } });
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createVendor).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Create Vendor' })).not.toBeInTheDocument()
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('registers a beforeunload guard only while the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const addSpy = jest.spyOn(window, 'addEventListener');
    const { codeInput } = await openCreateVendorModal();

    const beforeUnloadCalls = () =>
      addSpy.mock.calls.filter(([type]) => type === 'beforeunload');

    expect(beforeUnloadCalls()).toHaveLength(0);

    fireEvent.change(codeInput, { target: { value: 'VND-099' } });
    await waitFor(() => expect(beforeUnloadCalls().length).toBeGreaterThan(0));
    addSpy.mockRestore();
  });
});

describe('Purchasing — Create PO unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  async function openCreatePOModal() {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    fireEvent.click(screen.getByRole('button', { name: /new po/i }));
    await screen.findByRole('heading', { name: 'Create Purchase Order' });
  }

  it('closes a clean form silently but prompts once it is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    await openCreatePOModal();

    // Clean: closes with no prompt.
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Create Purchase Order' })).not.toBeInTheDocument()
    );

    // Reopen and dirty it (Notes is the cheapest field).
    fireEvent.click(screen.getByRole('button', { name: /new po/i }));
    const notes = await screen.findByLabelText('Notes');
    fireEvent.change(notes, { target: { value: 'rush order' } });

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    // Declined: still open with the edit preserved.
    expect(screen.getByLabelText('Notes')).toHaveValue('rush order');
  });
});

describe('Purchasing — Edit Vendor unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  async function openEditVendorModal() {
    renderPurchasing();
    await screen.findByRole('heading', { name: 'Purchasing & Receiving' });
    fireEvent.click(screen.getByRole('button', { name: /^Vendors/i }));
    const row = (await screen.findByText('Acme Aerospace')).closest('tr')!;
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    await screen.findByRole('heading', { name: 'Edit Vendor' });
  }

  it('treats the prefilled form as clean and only prompts after an edit', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    await openEditVendorModal();

    // Prefilled-but-untouched: closes silently (snapshot == current).
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Edit Vendor' })).not.toBeInTheDocument()
    );

    // Reopen, edit a field — now every close path prompts.
    const row = (await screen.findByText('Acme Aerospace')).closest('tr')!;
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    await screen.findByRole('heading', { name: 'Edit Vendor' });
    fireEvent.change(screen.getByLabelText(/Vendor Name/), { target: { value: 'Acme Aero LLC' } });

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Vendor Name/)).toHaveValue('Acme Aero LLC');
    expect(mockedApi.updateVendor).not.toHaveBeenCalled();
  });
});
