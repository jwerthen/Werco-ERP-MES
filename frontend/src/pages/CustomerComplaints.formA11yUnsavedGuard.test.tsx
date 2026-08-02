/**
 * CustomerComplaints — useUnsavedChanges discard guard on the create-complaint
 * (and RMA) modals.
 *
 * Clones the Customers/Materials formA11yUnsavedGuard template:
 *   - clean (as-opened) form -> Cancel closes with NO confirm prompt (the
 *     snapshot is captured on open, so the defaulted date_received does not
 *     count as dirty),
 *   - dirty + declined       -> modal stays open, the entry is preserved,
 *   - dirty + confirmed      -> modal closes, nothing created,
 *   - successful SAVE        -> closes directly, NO prompt,
 *   - a beforeunload listener is registered only while the form is dirty.
 *
 * The guard is wired into the Modal onClose (backdrop clicks included — this
 * modal allows closeOnBackdrop), not just the Cancel button.
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
