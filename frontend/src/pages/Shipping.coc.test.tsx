/**
 * The numbered certificate and the open quality items on the Shipping page
 * (shipping/1 + shipping/2): a row with an issued CoC links its number to the issued
 * PDF; a withheld one says Quality is to issue it; Issue CoC appears only for Admin,
 * Manager or Quality on a shipped row with none; Ready to Ship shows the open items as
 * chips; Ship opens the server-gated dialog instead of shipping on the click.
 */
import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Shipping from './Shipping';
import api from '../services/api';
import type { ReadyToShipRow, ShipmentSummary } from '../types/shipping';

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
jest.mock('../components/shipping/CocPdfPreviewModal', () => ({
  __esModule: true,
  default: ({ cocNumber }: { cocNumber: string }) => <div role="dialog" aria-label={`Preview ${cocNumber}`} />,
}));

const mocked = api as jest.Mocked<typeof api>;

const base: ShipmentSummary = {
  id: 1,
  shipment_number: 'SHP-COC-1',
  work_order_id: 11,
  work_order_number: 'WO-COC-11',
  customer_name: 'Example Aero Co',
  part_number: 'PART-COC',
  status: 'shipped',
  quantity_shipped: 4,
  ship_date: '2026-11-20',
  created_at: '2026-11-19T15:00:00Z',
  coc_number: null,
  coc_issued_at: null,
  coc_withheld: false,
};
const issued: ShipmentSummary = {
  ...base,
  coc_number: 'COC-2026-0042',
  coc_issued_at: '2026-11-20T16:00:00Z',
};
const withheld: ShipmentSummary = { ...base, id: 2, shipment_number: 'SHP-COC-2', coc_withheld: true };
const pending: ShipmentSummary = { ...base, id: 3, shipment_number: 'SHP-COC-3', status: 'pending', ship_date: null };
const ready: ReadyToShipRow = {
  work_order_id: 21,
  work_order_number: 'WO-READY-21',
  part_number: 'PART-READY',
  customer_name: 'Example Aero Co',
  quantity_complete: 5,
  quantity_remaining: 5,
  quantity_reserved: 0,
  quantity_shipped: 0,
  release_exceptions: [
    {
      code: 'open_ncr',
      severity: 'high',
      message: 'NCR NCR-0101 on this work order is open (status=open, disposition=pending).',
      reference_type: 'ncr',
      reference_id: 101,
    },
  ],
};

const view = () =>
  render(
    <MemoryRouter>
      <Shipping />
    </MemoryRouter>
  );

/** The desktop table row for a shipment (mobile cards render the same actions too). */
async function desktopRow(shipmentNumber: string) {
  const cells = await screen.findAllByText(shipmentNumber);
  const row = cells.map(cell => cell.closest('tr')).find(Boolean);
  if (!row) throw new Error(`no table row for ${shipmentNumber}`);
  return row as HTMLElement;
}

beforeEach(() => {
  jest.clearAllMocks();
  permissions.role = 'shipping';
  permissions.canComplete = true;
  mocked.getShipments.mockResolvedValue([issued, withheld, pending]);
  mocked.getReadyToShip.mockResolvedValue([ready]);
  mocked.getShipment.mockReturnValue(new Promise(() => undefined));
});

test('an issued CoC shows its number, which opens the issued PDF', async () => {
  view();
  const row = await desktopRow('SHP-COC-1');
  fireEvent.click(within(row).getByRole('button', { name: 'Download CoC COC-2026-0042' }));
  expect(screen.getByRole('dialog', { name: 'Preview COC-2026-0042' })).toBeInTheDocument();
});

test('a withheld CoC says Quality is to issue it; Shipping gets no Issue CoC', async () => {
  view();
  const row = await desktopRow('SHP-COC-2');
  expect(within(row).getByText('CoC withheld, Quality to issue')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Issue CoC/ })).not.toBeInTheDocument();
});

test('Quality sees Issue CoC on a shipped row with none, never on one already issued or still pending', async () => {
  permissions.role = 'quality';
  permissions.canComplete = false;
  view();
  const withheldRow = await desktopRow('SHP-COC-2');
  expect(within(withheldRow).getByRole('button', { name: /Issue CoC/ })).toBeInTheDocument();
  expect(within(await desktopRow('SHP-COC-1')).queryByRole('button', { name: /Issue CoC/ })).not.toBeInTheDocument();
  const pendingRow = await desktopRow('SHP-COC-3');
  expect(within(pendingRow).queryByRole('button', { name: /Issue CoC/ })).not.toBeInTheDocument();
  // Quality reaches Ship only to accept open items; the dialog decides.
  expect(within(pendingRow).getByRole('button', { name: /Ship/ })).toBeInTheDocument();

  fireEvent.click(within(withheldRow).getByRole('button', { name: /Issue CoC/ }));
  expect(screen.getByRole('heading', { name: 'Issue Certificate of Conformance for SHP-COC-2' })).toBeInTheDocument();
  expect(mocked.issueCoc).not.toHaveBeenCalled();
});

test('Ship opens the confirmation instead of shipping on the click', async () => {
  view();
  const row = await desktopRow('SHP-COC-3');
  fireEvent.click(within(row).getByRole('button', { name: /Ship/ }));
  expect(screen.getByRole('heading', { name: 'Ship SHP-COC-3' })).toBeInTheDocument();
  expect(mocked.getShipment).toHaveBeenCalledWith(3);
  expect(mocked.markShipped).not.toHaveBeenCalled();
});

test('Ready to Ship shows the open quality items as chips', async () => {
  view();
  const chips = await screen.findAllByRole('list', { name: 'Open quality items' });
  expect(chips.length).toBeGreaterThan(0);
  expect(within(chips[0]).getByText('Open NCR')).toBeInTheDocument();
});
