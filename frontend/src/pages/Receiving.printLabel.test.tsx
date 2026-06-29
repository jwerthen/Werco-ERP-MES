/**
 * Receiving — "Print label" action.
 *
 * Covers: the print button renders on inspection-queue rows for a privileged
 * role and is hidden for an operator (RBAC parity with the backend role gate),
 * clicking it calls api.printReceiptLabel and surfaces the server's success
 * message, and a 409 (egress disabled) maps to the helpful "isn't enabled" hint
 * rather than a raw error.
 *
 * The api service + AuthContext are mocked at the module boundary — no real
 * network (same pattern as the sibling page tests).
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
    printReceiptLabel: jest.fn(),
  },
}));

let mockAuthUser: { id: number; role: string } = { id: 1, role: 'manager' };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const QUEUE_ITEM = {
  receipt_id: 42,
  receipt_number: 'RCV-20260618-001',
  po_number: 'PO-1001',
  vendor_name: 'Acme Metals',
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_received: 10,
  lot_number: 'LOT-9',
  coc_attached: true,
  received_at: '2026-06-18T12:00:00Z',
  days_pending: 1,
};

const renderQueue = () =>
  render(
    <MemoryRouter initialEntries={['/receiving?tab=queue']}>
      <ReceivingPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  mockApi.getOpenPOsForReceiving.mockResolvedValue([]);
  mockApi.getReceivingLocations.mockResolvedValue([]);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 1,
    receipts_in_period: 1,
    acceptance_rate: 100,
    rejections_in_period: 0,
  });
  mockApi.getInspectionQueue.mockResolvedValue([QUEUE_ITEM]);
  mockApi.getReceivingHistory.mockResolvedValue([]);
});

// The inspection queue renders both a desktop <table> and a parallel mobile-card
// list (DataTable.mobileCards), so the receipt number appears twice in jsdom.
// Resolve the desktop table row (the only match inside a <tr>).
const findDesktopRow = async (text: string): Promise<HTMLElement> => {
  await waitFor(() => expect(screen.getAllByText(text).length).toBeGreaterThan(0));
  const cell = screen.getAllByText(text).find((el) => el.closest('tr') !== null);
  expect(cell).toBeTruthy();
  return (cell as HTMLElement).closest('tr') as HTMLElement;
};

describe('Receiving — print label button', () => {
  it('renders the Label button on a queue row for a privileged role and prints on click', async () => {
    mockApi.printReceiptLabel.mockResolvedValueOnce({
      receipt_id: 42,
      receipt_number: 'RCV-20260618-001',
      label_document_id: 9,
      printed: true,
      message: 'Label sent to printer',
    });
    renderQueue();

    const row = await findDesktopRow('RCV-20260618-001');
    const labelBtn = within(row).getByRole('button', { name: /label/i });
    fireEvent.click(labelBtn);

    await waitFor(() => expect(mockApi.printReceiptLabel).toHaveBeenCalledWith(42));
    await waitFor(() => expect(screen.getByText('Label sent to printer')).toBeInTheDocument());
  });

  it('shows a helpful hint when printing is not enabled (HTTP 409)', async () => {
    mockApi.printReceiptLabel.mockRejectedValueOnce(http(409, 'Print egress is disabled'));
    renderQueue();

    const row = await findDesktopRow('RCV-20260618-001');
    fireEvent.click(within(row).getByRole('button', { name: /label/i }));

    await waitFor(() =>
      expect(screen.getByText(/label printing isn't enabled/i)).toBeInTheDocument(),
    );
  });

  it('hides the Label button for an operator (RBAC parity)', async () => {
    mockAuthUser = { id: 2, role: 'operator' };
    renderQueue();

    const row = await findDesktopRow('RCV-20260618-001');
    expect(within(row).queryByRole('button', { name: /label/i })).toBeNull();
    // The Inspect button is unaffected.
    expect(within(row).getByRole('button', { name: /inspect/i })).toBeInTheDocument();
  });
});
