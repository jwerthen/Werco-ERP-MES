/**
 * Traceability — heat, cert and the mill-cert file follow a PO-received lot onto the
 * trace header (the server falls back to the receipt when the stock row is blank), and
 * the certificate file is reached by its document id, never a text search.
 */
import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Traceability from './Traceability';
import type { LotTraceResponse, TraceSearchResult } from '../types/traceability';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    searchLots: jest.fn(),
    traceLot: jest.fn(),
    traceSerial: jest.fn(),
    downloadDocument: jest.fn(),
    getDocument: jest.fn(),
  },
}));
let mockRole = 'quality';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: mockRole, is_superuser: false } }),
}));

const mockedApi = jest.mocked(api);

const lotTrace = (overrides: Partial<LotTraceResponse> = {}): LotTraceResponse => ({
  lot_number: 'LOT-H1',
  part_id: 7,
  part_number: 'PN-700',
  part_name: 'Plate 0.250 A36',
  supplier_name: 'Invented Plate Supply',
  po_number: 'PO-1001',
  received_date: '2026-11-20T15:00:00Z',
  cert_number: 'MTR-55',
  heat_lot: 'H-9931',
  certificate_document_id: 412,
  current_quantity: 12,
  current_location: 'STORES',
  status: 'available',
  work_orders_used: [],
  shipments: [],
  ncrs: [],
  consumed_components: [],
  history: [
    {
      timestamp: '2026-11-20T15:00:00Z',
      event_type: 'received',
      description: 'Received from PO PO-1001',
      quantity: 12,
      reference: 'RCV-1',
      details: {
        heat_number: 'H-9931',
        cert_number: 'MTR-55',
        certificate_document_id: 412,
        packing_slip: 'PS-1',
        inspection_status: 'passed',
      },
    },
  ],
  ...overrides,
});

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Traceability />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'quality';
});

test('the trace header shows heat and cert and reaches the certificate file by document id', async () => {
  mockedApi.traceLot.mockResolvedValue(lotTrace());
  renderAt('/traceability?number=LOT-H1&type=lot');

  expect(await screen.findByRole('heading', { name: 'Lot: LOT-H1' })).toBeInTheDocument();
  expect(mockedApi.traceLot).toHaveBeenCalledWith('LOT-H1');
  expect(screen.getAllByText('H-9931').length).toBeGreaterThan(0);
  expect(screen.getByText('MTR-55')).toBeInTheDocument();
  // The cert number is plain text now: no free-text Documents search link.
  expect(screen.queryByRole('link', { name: 'MTR-55' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Download certificate' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Open document record' })).toHaveAttribute('href', '/documents?document=412');
  // The received event repeats what the receipt recorded.
  expect(screen.getByText('Heat H-9931 · Cert MTR-55')).toBeInTheDocument();
});

test('no certificate file renders a dash and no download', async () => {
  mockedApi.traceLot.mockResolvedValue(lotTrace({ certificate_document_id: null, history: [] }));
  renderAt('/traceability?number=LOT-H1&type=lot');

  expect(await screen.findByRole('heading', { name: 'Lot: LOT-H1' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Download certificate' })).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Open document record' })).not.toBeInTheDocument();
});

test('the document-record link is hidden from a role that cannot open Documents', async () => {
  mockRole = 'no_such_role';
  mockedApi.traceLot.mockResolvedValue(lotTrace());
  renderAt('/traceability?number=LOT-H1&type=lot');

  expect(await screen.findByRole('button', { name: 'Download certificate' })).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Open document record' })).not.toBeInTheDocument();
});

test('search results name the heat and cert so a heat search can be told apart', async () => {
  const results: TraceSearchResult[] = [
    {
      type: 'lot',
      number: 'LOT-H1',
      part_number: 'PN-700',
      quantity: 12,
      location: 'STORES',
      heat_lot: 'H-9931',
      cert_number: 'MTR-55',
    },
    // A lot still held for inspection: found from its receipt, nothing on hand yet.
    {
      type: 'lot',
      number: 'LOT-H2',
      part_number: 'PN-701',
      quantity: 0,
      location: null,
      heat_lot: 'H-9931',
      cert_number: null,
    },
  ];
  mockedApi.searchLots.mockResolvedValue(results);
  renderAt('/traceability');

  fireEvent.change(screen.getByLabelText(/search by lot/i), { target: { value: 'H-9931' } });
  fireEvent.click(screen.getByRole('button', { name: /trace/i }));

  const first = (await screen.findByText('LOT-H1')).closest('button')!;
  expect(within(first).getByText('Heat H-9931 · Cert MTR-55')).toBeInTheDocument();
  const held = screen.getByText('LOT-H2').closest('button')!;
  expect(within(held).getByText('Heat H-9931')).toBeInTheDocument();
  expect(within(held).getByText('Qty: 0')).toBeInTheDocument();
  expect(mockedApi.traceLot).not.toHaveBeenCalled();
});

test('a received event with no heat or cert on its receipt adds no heat/cert line', async () => {
  const trace = lotTrace({ heat_lot: null, cert_number: null });
  trace.history[0].details = { heat_number: '  ', cert_number: null, certificate_document_id: null };
  mockedApi.traceLot.mockResolvedValue(trace);
  renderAt('/traceability?number=LOT-H1&type=lot');

  expect(await screen.findByRole('heading', { name: 'Lot: LOT-H1' })).toBeInTheDocument();
  // The timeline event renders its description, and nothing for a blank heat / cert.
  expect(screen.getByText('Received from PO PO-1001')).toBeInTheDocument();
  const heatCertLine = screen.queryAllByText(
    (_, el) => el?.tagName === 'P' && /^(Heat|Cert) /.test(el.textContent ?? '')
  );
  expect(heatCertLine).toHaveLength(0);
});

test('a serial trace carries no certificate file and calls the serial endpoint', async () => {
  mockedApi.traceSerial.mockResolvedValue({
    serial_number: 'SN-1',
    part_number: 'PN-700',
    lot_number: 'LOT-H1',
    status: 'available',
    cert_number: 'MTR-55',
    work_orders_used: [],
    ncrs: [],
    history: [],
  });
  renderAt('/traceability?number=SN-1&type=serial');

  expect(await screen.findByRole('heading', { name: 'Serial: SN-1' })).toBeInTheDocument();
  expect(mockedApi.traceSerial).toHaveBeenCalledWith('SN-1');
  expect(mockedApi.traceLot).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Download certificate' })).not.toBeInTheDocument();
});
