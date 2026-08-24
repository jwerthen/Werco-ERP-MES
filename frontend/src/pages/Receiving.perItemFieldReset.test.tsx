/**
 * Receiving — per-item fields do not carry over from the previously received part.
 *
 * `handleSelectLine` spreads the previous `formData` so shipment-level fields (packing
 * slip #, carrier, tracking #, location) stay filled across every line of one delivery.
 * That spread used to carry the MATERIAL-level fields too, so each new part opened with
 * the last part's lot and heat already in the boxes and the receiver had to delete them
 * (owner report, 2026-08-24).
 *
 * These tests pin both halves of the split — what must be blank, and what must survive —
 * plus the safety case the same spread created: `over_receive_approved` is an approval
 * whose checkbox is HIDDEN while the quantity fits the line, so a sticky `true` would
 * satisfy handleReceive's over-receipt guard on the next line with nobody clicking it.
 *
 * The api service + AuthContext are mocked at the module boundary — no real network
 * (same pattern as the sibling Receiving page tests).
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

const line = (lineId: number, lineNumber: number, partNumber: string, remaining: number) => ({
  line_id: lineId,
  line_number: lineNumber,
  part_id: lineId,
  part_number: partNumber,
  part_name: `${partNumber} part`,
  quantity_ordered: remaining,
  quantity_received: 0,
  quantity_remaining: remaining,
  unit_price: 10,
  required_date: null,
  is_closed: false,
});

/** Two open lines on one PO — the multi-part delivery the owner was receiving. */
const twoLinePO = () => ({
  po_id: 1,
  po_number: 'PO-1001',
  vendor_id: 3,
  vendor_name: 'Acme Metals',
  vendor_code: 'VND-001',
  order_date: null,
  required_date: null,
  expected_date: null,
  status: 'sent',
  lines: [line(100, 1, 'PN-A', 5), line(101, 2, 'PN-B', 4)],
  total_lines: 2,
});

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/receiving']}>
      <ReceivingPage />
    </MemoryRouter>,
  );

/** Open the receive modal for the PO line whose row shows `partNumber`. */
const openReceiveModalFor = async (partNumber: string): Promise<HTMLElement> => {
  const row = (await screen.findByText(partNumber)).closest('tr') as HTMLElement;
  fireEvent.click(within(row).getByRole('button', { name: 'Receive' }));
  return screen.findByRole('dialog');
};

beforeEach(() => {
  jest.clearAllMocks();
  const po = twoLinePO();
  mockApi.getOpenPOsForReceiving.mockResolvedValue([po]);
  mockApi.getPOForReceiving.mockResolvedValue(po);
  mockApi.getReceivingLocations.mockResolvedValue([]);
  mockApi.getReceivingStats.mockResolvedValue({
    pending_inspection: 0,
    receipts_in_period: 0,
    acceptance_rate: 100,
    rejections_in_period: 0,
  });
  mockApi.getInspectionQueue.mockResolvedValue([]);
  mockApi.getReceivingHistory.mockResolvedValue([]);
  mockApi.receiveNewMaterial.mockResolvedValue({ id: 99, receipt_number: 'RCV-20260824-001' });
});

describe('Receiving — per-item fields reset between parts', () => {
  it('blanks lot/heat/cert/CoC/notes for the next part, and keeps the shipment fields', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('PO-1001'));

    // --- Part A: fill both the material fields and the shipment fields. ---
    const first = await openReceiveModalFor('PN-A');
    fireEvent.change(within(first).getByLabelText(/^Lot Number$/i), { target: { value: 'LOT-A' } });
    fireEvent.change(within(first).getByLabelText(/^Heat Number$/i), { target: { value: 'HEAT-A' } });
    fireEvent.change(within(first).getByLabelText(/^Cert Number$/i), { target: { value: 'CERT-A' } });
    fireEvent.change(within(first).getByLabelText(/^Notes$/i), { target: { value: 'dinged corner' } });
    fireEvent.click(within(first).getByLabelText('CoC Attached'));
    fireEvent.change(within(first).getByLabelText(/^Packing Slip #$/i), { target: { value: 'PS-777' } });
    fireEvent.change(within(first).getByLabelText(/^Carrier$/i), { target: { value: 'UPS' } });
    fireEvent.change(within(first).getByLabelText(/^Tracking Number$/i), { target: { value: '1Z-999' } });
    fireEvent.click(within(first).getByRole('button', { name: 'Receive Material' }));

    await waitFor(() =>
      expect(mockApi.receiveNewMaterial).toHaveBeenCalledWith(
        expect.objectContaining({
          po_line_id: 100,
          lot_number: 'LOT-A',
          heat_number: 'HEAT-A',
          cert_number: 'CERT-A',
          coc_attached: true,
        }),
      ),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    // --- Part B: the material fields must open blank, no deleting required. ---
    const second = await openReceiveModalFor('PN-B');
    expect(within(second).getByLabelText(/^Lot Number$/i)).toHaveValue('');
    expect(within(second).getByLabelText(/^Heat Number$/i)).toHaveValue('');
    expect(within(second).getByLabelText(/^Cert Number$/i)).toHaveValue('');
    expect(within(second).getByLabelText(/^Notes$/i)).toHaveValue('');
    expect(within(second).getByLabelText('CoC Attached')).not.toBeChecked();
    expect(within(second).getByLabelText('Requires Inspection')).not.toBeChecked();

    // --- ...while the one-delivery shipment fields deliberately stay filled. ---
    expect(within(second).getByLabelText(/^Packing Slip #$/i)).toHaveValue('PS-777');
    expect(within(second).getByLabelText(/^Carrier$/i)).toHaveValue('UPS');
    expect(within(second).getByLabelText(/^Tracking Number$/i)).toHaveValue('1Z-999');

    // And a blank lot still submits — the backend auto-assigns the receipt number.
    fireEvent.click(within(second).getByRole('button', { name: 'Receive Material' }));
    await waitFor(() =>
      expect(mockApi.receiveNewMaterial).toHaveBeenLastCalledWith(
        expect.objectContaining({
          po_line_id: 101,
          lot_number: undefined,
          heat_number: '',
          cert_number: '',
          coc_attached: false,
          packing_slip_number: 'PS-777',
        }),
      ),
    );
  });

  it('does not carry an over-receipt approval onto the next part', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('PO-1001'));

    // Part A: over-receive 9 against a remaining of 5, approved deliberately.
    const first = await openReceiveModalFor('PN-A');
    fireEvent.change(within(first).getByLabelText(/Quantity Received/i), { target: { value: '9' } });
    fireEvent.click(await within(first).findByLabelText('Approve Over-Receipt'));
    fireEvent.click(within(first).getByRole('button', { name: 'Receive Material' }));

    await waitFor(() =>
      expect(mockApi.receiveNewMaterial).toHaveBeenCalledWith(
        expect.objectContaining({ po_line_id: 100, quantity_received: 9, over_receive_approved: true }),
      ),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    // Part B: the approval must NOT still be in effect. Its checkbox is hidden until
    // the quantity exceeds the line, which is exactly why a sticky true was invisible.
    const second = await openReceiveModalFor('PN-B');
    fireEvent.change(within(second).getByLabelText(/Quantity Received/i), { target: { value: '9' } });
    expect(await within(second).findByLabelText('Approve Over-Receipt')).not.toBeChecked();

    fireEvent.click(within(second).getByRole('button', { name: 'Receive Material' }));
    // The validation banner is page-level, not inside the dialog.
    expect(await screen.findByText(/Quantity exceeds remaining \(4\)/i)).toBeInTheDocument();
    // The refusal is the point: no second receipt was posted.
    expect(mockApi.receiveNewMaterial).toHaveBeenCalledTimes(1);
  });
});
