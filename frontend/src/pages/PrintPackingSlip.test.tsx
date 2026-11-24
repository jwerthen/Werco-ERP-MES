/**
 * The packing slip prints the order facts (PO line, revision, unit, serials, ship-to
 * city/state/zip) and the issued certificate's number, and opens the issued PDF for
 * printing. It no longer draws its own look-alike Certificate of Conformance.
 */
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import PrintPackingSlip from './PrintPackingSlip';
import api from '../services/api';
import type { ShipmentDetail } from '../types/shipping';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getShipment: jest.fn() },
}));
jest.mock('../components/shipping/CocPdfPreviewModal', () => ({
  __esModule: true,
  default: ({ shipmentId, cocNumber }: { shipmentId: number; cocNumber: string }) => (
    <div role="dialog" aria-label={`Preview ${cocNumber} for ${shipmentId}`} />
  ),
}));

const mocked = api as jest.Mocked<typeof api>;

const shipment: ShipmentDetail = {
  id: 9,
  shipment_number: 'SHP-SLIP-9',
  work_order_id: 3,
  work_order_number: 'WO-SLIP-3',
  customer_name: 'Example Aero Co',
  customer_po: 'PO-SLIP-1',
  part_number: 'PART-SLIP',
  part_name: 'Test bracket',
  lot_number: 'LOT-SLIP',
  status: 'shipped',
  ship_to_name: 'Example Aero Co',
  ship_to_address: '100 Sample Way',
  ship_to_city: 'Springfield',
  ship_to_state: 'KS',
  ship_to_zip: '67000',
  po_line_item: '3',
  unit_number: 'U-17',
  part_revision: 'C',
  serial_numbers: ['SN-001', 'SN-002'],
  carrier: 'UPS',
  tracking_number: null,
  quantity_shipped: 2,
  weight_lbs: null,
  num_packages: 1,
  ship_date: '2026-11-20',
  cert_of_conformance: true,
  packing_notes: null,
  created_at: '2026-11-19T15:00:00Z',
  coc_number: 'COC-2026-0042',
  coc_issued_at: '2026-11-20T16:00:00Z',
  coc_withheld: false,
  release_exceptions: [],
};

const view = () =>
  render(
    <MemoryRouter initialEntries={['/print/packing-slip/9']}>
      <Routes>
        <Route path="/print/packing-slip/:id" element={<PrintPackingSlip />} />
      </Routes>
    </MemoryRouter>
  );

let printSpy: jest.SpyInstance;
beforeEach(() => {
  jest.clearAllMocks();
  printSpy = jest.spyOn(window, 'print').mockImplementation(() => undefined);
});
afterEach(() => printSpy.mockRestore());

test('prints the order facts and the issued certificate number, with no look-alike certificate', async () => {
  mocked.getShipment.mockResolvedValue(shipment);
  view();

  expect(await screen.findByRole('heading', { name: 'PACKING SLIP' })).toBeInTheDocument();
  expect(screen.getByText('Springfield, KS 67000')).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'PO Line' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Rev' })).toBeInTheDocument();
  expect(screen.getByText('C')).toBeInTheDocument();
  expect(screen.getByText('U-17')).toBeInTheDocument();
  expect(screen.getByText('SN-001, SN-002')).toBeInTheDocument();
  expect(screen.getByText(/^COC-2026-0042 \(issued /)).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: 'CERTIFICATE OF CONFORMANCE' })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Print certificate' }));
  expect(screen.getByRole('dialog', { name: 'Preview COC-2026-0042 for 9' })).toBeInTheDocument();
});

test('a withheld certificate says Quality is to issue it, and there is nothing to print', async () => {
  mocked.getShipment.mockResolvedValue({ ...shipment, coc_number: null, coc_issued_at: null, coc_withheld: true });
  view();

  expect(await screen.findByText('Withheld, Quality to issue')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Print certificate' })).not.toBeInTheDocument();
});
