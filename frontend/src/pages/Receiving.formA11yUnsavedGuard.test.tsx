/**
 * Receiving — useUnsavedChanges discard guard on the Receive Material modal.
 *
 * Clones the Customers/Materials formA11yUnsavedGuard template:
 *   - clean (as-opened) form -> Cancel closes with NO confirm prompt (the
 *     snapshot is captured when the line is selected, so the prefilled
 *     quantity does not count as dirty),
 *   - dirty + declined       -> modal stays open, the entry is preserved,
 *   - dirty + confirmed      -> modal closes, nothing received,
 *   - successful RECEIVE     -> closes directly, NO prompt,
 *   - a beforeunload listener is registered only while the form is dirty.
 *
 * The guard is wired into the Modal onClose (header X / Escape included), not
 * just the Cancel button.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ReceivingPage from './Receiving';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getOpenPOsForReceiving: jest.fn(),
    getReceivingLocations: jest.fn(),
    getReceivingStats: jest.fn(),
    getInspectionQueue: jest.fn(),
    getReceivingHistory: jest.fn(),
    getPOForReceiving: jest.fn(),
    receiveNewMaterial: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const line = {
  line_id: 100,
  line_number: 1,
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_ordered: 10,
  quantity_received: 5,
  quantity_remaining: 5,
  unit_price: 12.5,
  required_date: null,
  is_closed: false,
};

const po = {
  po_id: 1,
  po_number: 'PO-1001',
  vendor_id: 3,
  vendor_name: 'Acme Metals',
  vendor_code: 'VND-001',
  order_date: null,
  required_date: null,
  expected_date: null,
  status: 'sent',
  lines: [line],
  total_lines: 1,
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/receiving']}>
      <ReceivingPage />
    </MemoryRouter>,
  );

/** Load the page, select the PO + line, and open the Receive modal. */
async function openReceiveModal() {
  renderPage();
  fireEvent.click(await screen.findByText('PO-1001'));
  fireEvent.click(await screen.findByRole('button', { name: 'Receive' }));
  const dialog = await screen.findByRole('dialog');
  const lotInput = screen.getByLabelText(/^Lot Number$/i);
  return { dialog, lotInput };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getOpenPOsForReceiving.mockResolvedValue([po] as any);
  mockApi.getReceivingLocations.mockResolvedValue([] as any);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 0,
    receipts_in_period: 0,
    acceptance_rate: 100,
    rejections_in_period: 0,
  } as any);
  mockApi.getInspectionQueue.mockResolvedValue([] as any);
  mockApi.getReceivingHistory.mockResolvedValue([] as any);
  mockApi.getPOForReceiving.mockResolvedValue(po as any);
});

describe('Receiving — Receive Material unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing the as-opened (clean) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openReceiveModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('prompts and keeps the modal open when the user declines the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { lotInput } = await openReceiveModal();

    fireEvent.change(lotInput, { target: { value: 'LOT-777' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^Lot Number$/i)).toHaveValue('LOT-777');
  });

  it('prompts and closes (discarding the entry) when the user confirms', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const { lotInput } = await openReceiveModal();

    fireEvent.change(lotInput, { target: { value: 'LOT-777' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockApi.receiveNewMaterial).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful receive even though the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockApi.receiveNewMaterial.mockResolvedValue({ id: 99, receipt_number: 'RCV-001' } as any);
    const { dialog, lotInput } = await openReceiveModal();

    fireEvent.change(lotInput, { target: { value: 'LOT-777' } });
    // Scope to the dialog: the page also has a "Receive Material" tab button.
    fireEvent.click(within(dialog).getByRole('button', { name: /receive material/i }));

    await waitFor(() => expect(mockApi.receiveNewMaterial).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('registers a beforeunload guard only while the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const addSpy = jest.spyOn(window, 'addEventListener');
    const { lotInput } = await openReceiveModal();

    const beforeUnloadCalls = () =>
      addSpy.mock.calls.filter(([type]) => type === 'beforeunload');

    expect(beforeUnloadCalls()).toHaveLength(0);

    fireEvent.change(lotInput, { target: { value: 'LOT-777' } });
    await waitFor(() => expect(beforeUnloadCalls().length).toBeGreaterThan(0));
    addSpy.mockRestore();
  });
});
