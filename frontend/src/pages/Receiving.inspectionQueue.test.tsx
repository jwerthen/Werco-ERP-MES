/**
 * Receiving — inspection-queue scope, Inspect gating, receive default,
 * and History who/when rendering.
 *
 * Covers the go-live receiving fixes:
 *  - the queue is fetched WITHOUT a date cutoff (api.getInspectionQueue takes
 *    no argument, so pending receipts older than 30 days still surface)
 *  - an orphaned queue row (null PO/part context from the backend's degraded
 *    serialization) renders placeholders, never the literal string "null"
 *  - the Inspect action is permission-gated (receiving:inspect): a supervisor
 *    now sees it, mirroring the backend role gate
 *  - the Receive form's "Requires Inspection" checkbox ALWAYS starts unchecked
 *    (owner-requested "default to no inspection" — reset on every line select,
 *    no within-session stickiness); a part flagged in the PART MASTER renders
 *    an amber advisory hint next to the checkbox instead of pre-checking it
 *  - the History tab renders who received (received_by_name) and the full
 *    date AND time in Central time
 *
 * The api service + AuthContext are mocked at the module boundary — no real
 * network (same pattern as Receiving.printLabel.test.tsx).
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
  },
}));

let mockAuthUser: { id: number; role: string } = { id: 1, role: 'manager' };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));

const mockApi = api as jest.Mocked<typeof api>;

const QUEUE_ITEM = {
  receipt_id: 42,
  receipt_number: 'RCV-20260618-001',
  po_number: 'PO-1001',
  po_id: 11,
  vendor_name: 'Acme Metals',
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_received: 10,
  lot_number: 'LOT-9',
  coc_attached: true,
  received_at: '2026-06-18T12:00:00Z',
  days_pending: 28,
};

// The degraded shape the backend returns for an orphaned receipt (missing PO
// line / purchase order / part): all PO/part context is null.
const ORPHAN_QUEUE_ITEM = {
  receipt_id: 43,
  receipt_number: 'RCV-20260618-009',
  po_number: null,
  po_id: null,
  vendor_name: null,
  part_id: null,
  part_number: null,
  part_name: null,
  quantity_received: 3,
  lot_number: 'LOT-ORPHAN',
  coc_attached: false,
  received_at: null,
  days_pending: 0,
};

const HISTORY_ITEM = {
  receipt_id: 77,
  receipt_number: 'RCV-20260618-002',
  po_number: 'PO-1002',
  part_number: 'PN-777',
  quantity_received: 5,
  quantity_accepted: 5,
  quantity_rejected: 0,
  lot_number: 'LOT-77',
  inspection_status: 'accepted',
  status: 'accepted',
  received_at: '2026-06-18T12:00:00Z',
  received_by_name: 'Riley Dockhand',
};

const OPEN_PO = {
  po_id: 5,
  po_number: 'PO-2001',
  vendor_id: 1,
  vendor_name: 'Acme Metals',
  vendor_code: 'VND-001',
  order_date: null,
  required_date: null,
  expected_date: null,
  status: 'sent',
  lines: [
    {
      line_id: 51,
      line_number: 1,
      part_id: 7,
      part_number: 'PN-555',
      part_name: 'Bracket',
      quantity_ordered: 10,
      quantity_received: 0,
      quantity_remaining: 10,
      unit_price: 3.5,
      required_date: null,
      // Part master says no incoming inspection required.
      requires_inspection: false,
    },
  ],
  total_lines: 1,
};

// Same PO but the part master flags the part as requiring incoming inspection.
const OPEN_PO_INSPECTION_PART = {
  ...OPEN_PO,
  lines: [{ ...OPEN_PO.lines[0], requires_inspection: true }],
};

const renderTab = (tab: 'receive' | 'queue' | 'history') =>
  render(
    <MemoryRouter initialEntries={[`/receiving?tab=${tab}`]}>
      <ReceivingPage />
    </MemoryRouter>
  );

const findDesktopRow = async (text: string): Promise<HTMLElement> => {
  await waitFor(() => expect(screen.getAllByText(text).length).toBeGreaterThan(0));
  const cell = screen.getAllByText(text).find(el => el.closest('tr') !== null);
  expect(cell).toBeTruthy();
  return (cell as HTMLElement).closest('tr') as HTMLElement;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager' };
  mockApi.getOpenPOsForReceiving.mockResolvedValue([OPEN_PO]);
  mockApi.getReceivingLocations.mockResolvedValue([]);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 1,
    receipts_in_period: 1,
    acceptance_rate: 100,
    rejections_in_period: 0,
  });
  mockApi.getInspectionQueue.mockResolvedValue([QUEUE_ITEM]);
  mockApi.getReceivingHistory.mockResolvedValue([HISTORY_ITEM]);
  mockApi.getPOForReceiving.mockResolvedValue(OPEN_PO);
});

describe('Receiving — inspection queue', () => {
  it('fetches the queue without a date cutoff so old pending receipts surface', async () => {
    renderTab('queue');
    await findDesktopRow('RCV-20260618-001');
    expect(mockApi.getInspectionQueue).toHaveBeenCalledWith();
  });

  it('shows the Inspect button for a supervisor (receiving:inspect)', async () => {
    mockAuthUser = { id: 3, role: 'supervisor' };
    renderTab('queue');

    const row = await findDesktopRow('RCV-20260618-001');
    expect(within(row).getByRole('button', { name: /inspect/i })).toBeInTheDocument();
  });

  it('renders placeholders (never "null") for an orphaned queue row', async () => {
    mockApi.getInspectionQueue.mockResolvedValue([ORPHAN_QUEUE_ITEM]);
    renderTab('queue');

    const row = await findDesktopRow('RCV-20260618-009');
    expect(within(row).queryByText(/null/i)).toBeNull();
    // PO/Vendor and Part cells degrade to an em-dash placeholder.
    expect(within(row).getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });
});

describe('Receiving — receive form default', () => {
  const HINT_TEXT = /part master flags this part as requiring incoming inspection/i;

  const openReceiveForm = async () => {
    fireEvent.click(await screen.findByRole('button', { name: /PO-2001/ }));
    await waitFor(() => expect(mockApi.getPOForReceiving).toHaveBeenCalledWith(5));
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }));
    return screen.findByRole('checkbox', { name: /requires inspection/i });
  };

  it('starts "Requires Inspection" UNCHECKED with no hint for an unflagged part', async () => {
    renderTab('receive');

    const checkbox = await openReceiveForm();
    expect(checkbox).not.toBeChecked();
    expect(screen.queryByText(HINT_TEXT)).toBeNull();
  });

  it('still starts UNCHECKED for a part-master-flagged part, but shows the advisory hint', async () => {
    mockApi.getPOForReceiving.mockResolvedValue(OPEN_PO_INSPECTION_PART);
    renderTab('receive');

    const checkbox = await openReceiveForm();
    // The part flag never pre-checks the box — the receiver opts in.
    expect(checkbox).not.toBeChecked();
    expect(screen.getByText(HINT_TEXT)).toBeInTheDocument();
  });

  it('resets to UNCHECKED on every line select (no within-session stickiness)', async () => {
    renderTab('receive');

    const checkbox = await openReceiveForm();
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();

    // Close the modal without receiving, then re-open the same line.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Receive' }));

    const reopened = await screen.findByRole('checkbox', { name: /requires inspection/i });
    expect(reopened).not.toBeChecked();
  });
});

describe('Receiving — history tab', () => {
  it('renders who received and the date AND time (Central)', async () => {
    renderTab('history');

    const row = await findDesktopRow('RCV-20260618-002');
    expect(within(row).getByText('Riley Dockhand')).toBeInTheDocument();
    // 2026-06-18T12:00:00Z is 7:00 AM Central (CDT).
    expect(within(row).getByText(/Jun 18, 2026/)).toBeInTheDocument();
    expect(within(row).getByText(/7:00/)).toBeInTheDocument();
  });
});
