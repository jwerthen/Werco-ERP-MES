/**
 * CustomerComplaints — useUnsavedChanges discard guard on BOTH form modals:
 * create-complaint AND Create RMA (each has its own covered trio below).
 *
 * Clones the Customers/Materials formA11yUnsavedGuard template:
 *   - clean (as-opened) form -> Cancel closes with NO confirm prompt (the
 *     snapshot is captured on open, so prefilled values don't count as dirty —
 *     the create modal's defaulted date_received, and the RMA modal's
 *     ENTIRELY server-prefilled body: customer/quantity/lot/reason all copied
 *     from the complaint; that prefilled-clean case is the load-bearing one,
 *     since a naive blank-comparison dirty check would flag it immediately),
 *   - dirty + declined       -> modal stays open, the entry is preserved,
 *   - dirty + confirmed      -> modal closes, nothing created,
 *   - successful SAVE        -> closes directly, NO prompt,
 *   - a beforeunload listener is registered only while the form is dirty.
 *
 * The guard is wired into the Modal onClose (backdrop clicks included — these
 * modals allow closeOnBackdrop), not just the Cancel button.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CustomerComplaints from './CustomerComplaints';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getComplaints: jest.fn(),
    getComplaintsDashboard: jest.fn(),
    createComplaint: jest.fn(),
    createRMA: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/complaints']}>
      <CustomerComplaints />
    </MemoryRouter>
  );

/** Load the page and open the New Complaint modal; returns the name input + form. */
async function openCreateModal() {
  renderPage();
  fireEvent.click((await screen.findAllByRole('button', { name: /new complaint/i }))[0]);
  await screen.findByRole('heading', { name: 'New Customer Complaint' });
  const nameInput = screen.getByLabelText(/Customer Name/);
  return { nameInput, form: nameInput.closest('form') as HTMLFormElement };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getComplaints.mockResolvedValue([] as any);
  mockedApi.getComplaintsDashboard.mockResolvedValue({
    total_open: 0,
    overdue_response: 0,
    avg_resolution_days: 0,
    by_severity: {},
    recent: [],
  } as any);
});

describe('CustomerComplaints — create form unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing the as-opened (clean) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openCreateModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New Customer Complaint' })).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user declines the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { nameInput } = await openCreateModal();

    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Customer Name/)).toHaveValue('Acme Aerospace');
  });

  it('prompts and closes (discarding the entry) when the user confirms', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const { nameInput } = await openCreateModal();

    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New Customer Complaint' })).not.toBeInTheDocument()
    );
    expect(mockedApi.createComplaint).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful save even though the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockedApi.createComplaint.mockResolvedValue({ id: 1 } as any);
    const { nameInput, form } = await openCreateModal();

    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    fireEvent.change(screen.getByLabelText(/^Title/), { target: { value: 'Late shipment damage' } });
    fireEvent.change(screen.getByLabelText(/^Description/), {
      target: { value: 'Two parts arrived dented.' },
    });
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createComplaint).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New Customer Complaint' })).not.toBeInTheDocument()
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('registers a beforeunload guard only while the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const addSpy = jest.spyOn(window, 'addEventListener');
    const { nameInput } = await openCreateModal();

    const beforeUnloadCalls = () =>
      addSpy.mock.calls.filter(([type]) => type === 'beforeunload');

    expect(beforeUnloadCalls()).toHaveLength(0);

    fireEvent.change(nameInput, { target: { value: 'Acme Aerospace' } });
    await waitFor(() => expect(beforeUnloadCalls().length).toBeGreaterThan(0));
    addSpy.mockRestore();
  });
});

describe('CustomerComplaints — Create RMA unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  // A complaint row to expand; the RMA form is prefilled ENTIRELY from it.
  const complaint = {
    id: 7,
    complaint_number: 'CC-2026-007',
    customer_name: 'Acme Aerospace',
    customer_contact: 'Pat Lee',
    customer_po_number: 'PO-88',
    lot_number: 'LOT-42',
    serial_number: '',
    quantity_affected: 3,
    severity: 'minor',
    status: 'received',
    title: 'Dented brackets',
    description: 'Three brackets arrived dented on one flange.',
    date_received: '2026-07-30',
    estimated_cost: 250,
    rmas: [],
  };

  beforeEach(() => {
    mockedApi.getComplaints.mockResolvedValue([complaint] as any);
  });

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  /** Expand the complaint row and open the Create RMA modal. The number
   *  renders in both the desktop table and the mobile card — click the table
   *  cell occurrence to expand. */
  async function openRMAModal() {
    renderPage();
    const cell = (await screen.findAllByText('CC-2026-007')).find(
      (el) => el.closest('td') !== null
    ) as HTMLElement;
    fireEvent.click(cell);
    fireEvent.click(await screen.findByRole('button', { name: /create rma/i }));
    await screen.findByRole('heading', { name: /Create RMA from CC-2026-007/ });
  }

  it('treats the fully server-prefilled form as CLEAN and closes silently', async () => {
    // The load-bearing case: every field (customer, quantity, lot, reason) is
    // copied from the complaint on open. The snapshot is captured from that
    // same prefill, so an untouched close must NOT prompt — a blank-shape
    // comparison would false-flag this form as dirty immediately.
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openRMAModal();

    // Reason is a required FormField, so its accessible label carries the required marker.
    expect(screen.getByLabelText(/Reason/)).toHaveValue(complaint.description);
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /Create RMA from/ })).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user declines the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    await openRMAModal();

    fireEvent.change(screen.getByLabelText('Notes'), { target: { value: 'ship replacement first' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Notes')).toHaveValue('ship replacement first');
  });

  it('prompts and closes (discarding the entry) when the user confirms', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openRMAModal();

    fireEvent.change(screen.getByLabelText('Notes'), { target: { value: 'ship replacement first' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /Create RMA from/ })).not.toBeInTheDocument()
    );
    expect(mockedApi.createRMA).not.toHaveBeenCalled();
  });
});
