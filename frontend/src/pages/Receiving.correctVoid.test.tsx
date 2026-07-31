/**
 * Receiving — Correct / Void receipt actions.
 *
 * Covers the receipt-correction feature wired into the Receiving page:
 *  - a Correct action loads the receipt detail, opens the modal, and on submit
 *    calls api.correctReceipt with the entered NEW quantity + reason, then
 *    refreshes the queue;
 *  - a Void action requires a reason (blank is blocked client-side) and calls
 *    api.voidReceipt with the id + reason;
 *  - a server 409/400 surfaces the verbatim `detail` in an error toast and leaves
 *    the row (no success toast, no dead state);
 *  - RBAC parity with the backend gate: Correct is [admin, manager, supervisor],
 *    Void is [admin, manager] — the controls don't render below their gate.
 *
 * The api service + AuthContext are mocked at the module boundary; the real
 * ToastProvider wraps the page so toast text is assertable (sibling-test pattern).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ReceivingPage from './Receiving';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getOpenPOsForReceiving: jest.fn(),
    getReceivingLocations: jest.fn(),
    getReceivingStats: jest.fn(),
    getInspectionQueue: jest.fn(),
    getReceivingHistory: jest.fn(),
    getReceiptDetail: jest.fn(),
    correctReceipt: jest.fn(),
    voidReceipt: jest.fn(),
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

const RECEIPT_NUMBER = 'RCV-20260618-001';
const QUEUE_ITEM = {
  receipt_id: 42,
  receipt_number: RECEIPT_NUMBER,
  po_number: 'PO-1001',
  vendor_name: 'Acme Metals',
  part_id: 7,
  part_number: 'PN-555',
  part_name: 'Bracket',
  quantity_received: 5,
  lot_number: 'LOT-9',
  coc_attached: true,
  received_at: '2026-06-18T12:00:00Z',
  days_pending: 1,
};

const RECEIPT_DETAIL = {
  id: 42,
  quantity_received: 5,
  lot_number: 'LOT-9',
  heat_number: '',
  cert_number: '',
  serial_numbers: '',
  notes: '',
};

const renderQueue = () =>
  render(
    <MemoryRouter initialEntries={['/receiving?tab=queue']}>
      <ToastProvider>
        <ReceivingPage />
      </ToastProvider>
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
  } as any);
  mockApi.getInspectionQueue.mockResolvedValue([QUEUE_ITEM] as any);
  mockApi.getReceivingHistory.mockResolvedValue([]);
  mockApi.getReceiptDetail.mockResolvedValue(RECEIPT_DETAIL as any);
});

// The queue renders a desktop table AND a parallel mobile-card list, so each
// row control's aria-label appears twice; the [0] instance opens the same modal.
const openControl = async (namePattern: RegExp) => {
  await waitFor(() => expect(screen.getAllByRole('button', { name: namePattern }).length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole('button', { name: namePattern })[0]);
};

describe('Receiving — correct receipt', () => {
  it('loads detail, submits the new quantity + reason, and refreshes the queue', async () => {
    mockApi.correctReceipt.mockResolvedValueOnce({ id: 42, quantity_received: 2 } as any);
    renderQueue();

    await openControl(/correct receipt/i);

    // Modal opens after getReceiptDetail resolves.
    await screen.findByRole('heading', { name: new RegExp(`Correct Receipt ${RECEIPT_NUMBER}`) });
    expect(mockApi.getReceiptDetail).toHaveBeenCalledWith(42);

    // Enter the NEW total quantity + a reason, then save.
    fireEvent.change(screen.getByLabelText(/Quantity Received/i), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/Reason for Correction/i), {
      target: { value: 'Miscounted the skid; only 2 arrived.' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save correction/i }));

    await waitFor(() =>
      expect(mockApi.correctReceipt).toHaveBeenCalledWith(
        42,
        expect.objectContaining({ quantity_received: 2, reason: 'Miscounted the skid; only 2 arrived.' }),
      ),
    );
    // The queue is reloaded after a successful correction (initial load + refresh).
    await waitFor(() => expect(mockApi.getInspectionQueue.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('surfaces a 409 detail toast and does not close on a server refusal', async () => {
    const detail = 'This receipt has already been inspected. Corrections after inspection must be handled via the NCR.';
    mockApi.correctReceipt.mockRejectedValueOnce(http(409, detail));
    renderQueue();

    await openControl(/correct receipt/i);
    await screen.findByRole('heading', { name: new RegExp(`Correct Receipt ${RECEIPT_NUMBER}`) });

    fireEvent.change(screen.getByLabelText(/Reason for Correction/i), { target: { value: 'try to fix' } });
    fireEvent.click(screen.getByRole('button', { name: /save correction/i }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
    // Modal stays open (row not closed) so the user can adjust.
    expect(screen.getByRole('heading', { name: new RegExp(`Correct Receipt ${RECEIPT_NUMBER}`) })).toBeInTheDocument();
  });
});

describe('Receiving — void receipt', () => {
  it('requires a reason before calling api.voidReceipt', async () => {
    renderQueue();
    await openControl(/void receipt/i);
    await screen.findByRole('heading', { name: new RegExp(`Void Receipt ${RECEIPT_NUMBER}`) });

    // Submit with a blank reason -> blocked client-side, no API call.
    fireEvent.click(screen.getByRole('button', { name: 'Void Receipt' }));
    expect(await screen.findByText(/a reason is required to void a receipt/i)).toBeInTheDocument();
    expect(mockApi.voidReceipt).not.toHaveBeenCalled();
  });

  it('calls api.voidReceipt with the id + reason on confirm', async () => {
    mockApi.voidReceipt.mockResolvedValueOnce({ message: `Receipt ${RECEIPT_NUMBER} voided` });
    renderQueue();
    await openControl(/void receipt/i);
    await screen.findByRole('heading', { name: new RegExp(`Void Receipt ${RECEIPT_NUMBER}`) });

    fireEvent.change(screen.getByLabelText(/Reason for Void/i), {
      target: { value: 'Duplicate receipt keyed by mistake.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Void Receipt' }));

    await waitFor(() =>
      expect(mockApi.voidReceipt).toHaveBeenCalledWith(42, 'Duplicate receipt keyed by mistake.'),
    );
  });

  it('surfaces a 400 detail toast on a server refusal', async () => {
    const detail = 'Cannot correct/void: the received stock for this lot has already been allocated or consumed.';
    mockApi.voidReceipt.mockRejectedValueOnce(http(400, detail));
    renderQueue();
    await openControl(/void receipt/i);
    await screen.findByRole('heading', { name: new RegExp(`Void Receipt ${RECEIPT_NUMBER}`) });

    fireEvent.change(screen.getByLabelText(/Reason for Void/i), { target: { value: 'attempt void' } });
    fireEvent.click(screen.getByRole('button', { name: 'Void Receipt' }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
  });
});

describe('Receiving — RBAC gating of correct/void', () => {
  it('hides both controls for an operator (below every gate)', async () => {
    mockAuthUser = { id: 2, role: 'operator' };
    renderQueue();
    // Wait for the queue to render.
    await waitFor(() => expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0));

    expect(screen.queryByRole('button', { name: /correct receipt/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /void receipt/i })).toBeNull();
  });

  it('shows Correct but hides Void for a supervisor (correct gate includes supervisor, void does not)', async () => {
    mockAuthUser = { id: 3, role: 'supervisor' };
    renderQueue();
    await waitFor(() => expect(screen.getAllByText(RECEIPT_NUMBER).length).toBeGreaterThan(0));

    expect(screen.getAllByRole('button', { name: /correct receipt/i }).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /void receipt/i })).toBeNull();
  });
});
