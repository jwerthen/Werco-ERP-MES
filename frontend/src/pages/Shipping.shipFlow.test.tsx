/**
 * The Shipping page end to end over the server-gated Ship and Issue CoC dialogs
 * (shipping/2). Rendered inside the real ToastProvider so what the person is TOLD is under
 * test: a clean ship is a success toast; a ship whose certificate was withheld is a
 * warning that names Quality; a refusal from the server stays in the dialog and never
 * becomes a success; Supervisor sees the open items but cannot accept them. The list is
 * re-read from the server after every write (non-optimistic).
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Shipping from './Shipping';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import type { ReleaseException, ShipmentDetail, ShipmentSummary } from '../types/shipping';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShipments: jest.fn(),
    getReadyToShip: jest.fn(),
    getShipment: jest.fn(),
    markShipped: jest.fn(),
    issueCoc: jest.fn(),
    updateShipment: jest.fn(),
    createShipment: jest.fn(),
  },
}));

const permissions: { role: string; canComplete: boolean } = { role: 'shipping', canComplete: true };
jest.mock('../hooks/usePermissions', () => ({
  __esModule: true,
  default: () => ({
    can: (permission: string) => (permission === 'shipping:complete' ? permissions.canComplete : true),
    role: permissions.role,
    isAdmin: permissions.role === 'admin',
  }),
}));
jest.mock('../components/shipping/ShipmentTrackingPanel', () => () => null);
jest.mock('../components/shipping/ScheduleShipmentModal', () => () => null);
jest.mock('../components/shipping/CocPdfPreviewModal', () => () => null);

const mocked = api as jest.Mocked<typeof api>;

const openNcr: ReleaseException = {
  code: 'open_ncr',
  severity: 'high',
  message: 'NCR NCR-0202 on this work order is open (status=open, disposition=pending).',
  reference_type: 'ncr',
  reference_id: 202,
};

const pendingRow: ShipmentSummary = {
  id: 5,
  shipment_number: 'SHP-FLOW-5',
  work_order_id: 15,
  work_order_number: 'WO-FLOW-15',
  customer_name: 'Example Aero Co',
  part_number: 'PART-FLOW',
  status: 'pending',
  quantity_shipped: 3,
  ship_date: null,
  created_at: '2026-11-19T15:00:00Z',
  coc_number: null,
  coc_issued_at: null,
  coc_withheld: false,
};
const withheldRow: ShipmentSummary = {
  ...pendingRow,
  id: 6,
  shipment_number: 'SHP-FLOW-6',
  status: 'shipped',
  ship_date: '2026-11-20',
  coc_withheld: true,
};

function detail(id: number, release_exceptions: ReleaseException[]): ShipmentDetail {
  return {
    id,
    shipment_number: `SHP-FLOW-${id}`,
    work_order_id: 15,
    work_order_number: 'WO-FLOW-15',
    customer_name: 'Example Aero Co',
    customer_po: 'PO-FLOW-1',
    part_number: 'PART-FLOW',
    part_name: 'Flow bracket',
    lot_number: null,
    status: 'pending',
    ship_to_name: 'Example Aero Co',
    ship_to_address: null,
    ship_to_city: null,
    ship_to_state: null,
    ship_to_zip: null,
    po_line_item: null,
    unit_number: null,
    part_revision: 'A',
    serial_numbers: [],
    carrier: null,
    tracking_number: null,
    quantity_shipped: 3,
    weight_lbs: null,
    num_packages: 1,
    ship_date: null,
    cert_of_conformance: true,
    packing_notes: null,
    created_at: '2026-11-19T15:00:00Z',
    coc_number: null,
    coc_issued_at: null,
    coc_withheld: false,
    release_exceptions,
  };
}

const view = () =>
  render(
    <ToastProvider>
      <MemoryRouter>
        <Shipping />
      </MemoryRouter>
    </ToastProvider>
  );

async function desktopRow(shipmentNumber: string) {
  const cells = await screen.findAllByText(shipmentNumber);
  const row = cells.map(cell => cell.closest('tr')).find(Boolean);
  if (!row) throw new Error(`no table row for ${shipmentNumber}`);
  return row as HTMLElement;
}

async function openShip() {
  fireEvent.click(within(await desktopRow('SHP-FLOW-5')).getByRole('button', { name: /Ship/ }));
  await waitFor(() => expect(screen.queryByText(/Checking open quality items/)).not.toBeInTheDocument());
}

function acceptWithReason(reason: string) {
  fireEvent.click(screen.getByRole('checkbox', { name: /I accept these open quality items/ }));
  fireEvent.change(screen.getByRole('textbox', { name: /Reason/ }), { target: { value: reason } });
}

beforeEach(() => {
  jest.clearAllMocks();
  permissions.role = 'shipping';
  permissions.canComplete = true;
  mocked.getShipments.mockResolvedValue([pendingRow, withheldRow]);
  mocked.getReadyToShip.mockResolvedValue([]);
  mocked.getShipment.mockImplementation(async (id: number) => detail(id, []));
});

test('a clean ship is a success toast and the list is re-read from the server', async () => {
  mocked.markShipped.mockResolvedValue({
    message: 'Shipment marked as shipped',
    shipment_number: 'SHP-FLOW-5',
    coc_withheld: false,
    release_exceptions: [],
  });
  view();
  await openShip();
  const loadsBefore = mocked.getShipments.mock.calls.length;

  fireEvent.click(screen.getByRole('button', { name: 'Mark shipped' }));

  expect(await screen.findByText('Shipment marked as shipped')).toBeInTheDocument();
  expect(mocked.markShipped).toHaveBeenCalledWith(5, undefined, undefined);
  await waitFor(() => expect(mocked.getShipments.mock.calls.length).toBeGreaterThan(loadsBefore));
  expect(screen.queryByRole('heading', { name: 'Ship SHP-FLOW-5' })).not.toBeInTheDocument();
});

test('Admin ships over open items: the acknowledgment is sent and a withheld CoC is a warning naming Quality', async () => {
  permissions.role = 'admin';
  mocked.getShipment.mockImplementation(async (id: number) => detail(id, [openNcr]));
  mocked.markShipped.mockResolvedValue({
    message: 'Shipment marked as shipped',
    shipment_number: 'SHP-FLOW-5',
    coc_withheld: true,
    release_exceptions: [openNcr],
  });
  view();
  await openShip();
  acceptWithReason('Deviation D-12 on file');

  fireEvent.click(screen.getByRole('button', { name: 'Accept and ship' }));

  const warning = await screen.findByText(/withheld for open quality items; Quality to issue/);
  expect(warning).toBeInTheDocument();
  expect(screen.queryByText('Shipment marked as shipped')).not.toBeInTheDocument();
  expect(mocked.markShipped).toHaveBeenCalledWith(5, undefined, {
    acknowledge_exceptions: true,
    reason: 'Deviation D-12 on file',
  });
});

test('Supervisor sees the open items but cannot accept them, so nothing is sent', async () => {
  permissions.role = 'supervisor';
  mocked.getShipment.mockImplementation(async (id: number) => detail(id, [openNcr]));
  view();
  await openShip();

  expect(screen.getByText(openNcr.message)).toBeInTheDocument();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(screen.getByText(/Admin, Manager or Quality must accept these items with a reason/)).toBeInTheDocument();
  const confirm = screen.getByRole('button', { name: 'Accept and ship' });
  expect(confirm).toBeDisabled();
  fireEvent.click(confirm);
  expect(mocked.markShipped).not.toHaveBeenCalled();
});

test('a server refusal stays in the dialog and is never reported as shipped', async () => {
  permissions.role = 'manager';
  mocked.getShipment.mockImplementation(async (id: number) => detail(id, [openNcr]));
  mocked.markShipped.mockRejectedValue({
    response: {
      status: 503,
      data: {
        detail: 'The record of the accepted quality items could not be saved; the shipment was not shipped. Retry.',
      },
    },
  });
  view();
  await openShip();
  acceptWithReason('Deviation D-13 on file');
  const loadsBefore = mocked.getShipments.mock.calls.length;

  fireEvent.click(screen.getByRole('button', { name: 'Accept and ship' }));

  expect(await screen.findByRole('alert')).toHaveTextContent('the shipment was not shipped');
  expect(screen.getByRole('heading', { name: 'Ship SHP-FLOW-5' })).toBeInTheDocument();
  expect(screen.queryByText('Shipment marked as shipped')).not.toBeInTheDocument();
  expect(screen.queryByText(/Quality to issue\./)).not.toBeInTheDocument();
  expect(mocked.getShipments.mock.calls.length).toBe(loadsBefore);
});

test('Quality issues the withheld certificate over the open items and is told its number', async () => {
  permissions.role = 'quality';
  permissions.canComplete = false;
  mocked.getShipment.mockImplementation(async (id: number) => ({
    ...detail(id, [openNcr]),
    status: 'shipped',
    coc_withheld: true,
  }));
  mocked.issueCoc.mockResolvedValue({
    id: 77,
    coc_number: 'COC-2026-0077',
    shipment_id: 6,
    work_order_id: 15,
    issued_at: '2026-11-21T16:00:00Z',
  });
  view();
  fireEvent.click(within(await desktopRow('SHP-FLOW-6')).getByRole('button', { name: /Issue CoC/ }));
  await waitFor(() => expect(screen.queryByText(/Checking open quality items/)).not.toBeInTheDocument());
  acceptWithReason('Rework verified at final');

  fireEvent.click(screen.getByRole('button', { name: 'Accept and issue' }));

  expect(await screen.findByText('Certificate of Conformance COC-2026-0077 issued')).toBeInTheDocument();
  expect(mocked.issueCoc).toHaveBeenCalledWith(6, { acknowledge_exceptions: true, reason: 'Rework verified at final' });
  expect(mocked.markShipped).not.toHaveBeenCalled();
});
