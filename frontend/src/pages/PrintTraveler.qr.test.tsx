/**
 * A0.4 traveler QR plumbing — /print/traveler/:id
 *
 * Locks: every traveler QR is a URL (backend resolve-action parses these).
 * (1) ONE header QR with payload {origin}/work-orders/{id} — the old second
 * WO:{number} header QR is gone; (2) one per-operation QR per routing step
 * with payload {origin}/shop-floor/operations?scan=OP:{operation_id}
 * (URL-encoded), keeping the human-readable OP:{id} caption for manual
 * entry; and (3) the print-control footer (UNCONTROLLED WHEN PRINTED,
 * printed-at, printed-by, part revision).
 */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import QRCode from 'qrcode';
import api from '../services/api';
import PrintTraveler from './PrintTraveler';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getPart: jest.fn(),
    getMaterialRequirements: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 7, first_name: 'Quinn', last_name: 'Printer', role: 'supervisor' },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

jest.mock('qrcode', () => ({
  __esModule: true,
  default: {
    toDataURL: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedToDataURL = QRCode.toDataURL as jest.Mock;

const WORK_ORDER = {
  id: 42,
  work_order_number: 'WO-2026-0042',
  part_id: 9,
  status: 'released',
  priority: 3,
  quantity_ordered: 25,
  quantity_complete: 0,
  customer_name: 'Acme Aero',
  customer_po: 'PO-1',
  due_date: '2026-07-01',
  operations: [
    {
      id: 101,
      sequence: 10,
      operation_number: 'OP10',
      name: 'Laser Cut',
      work_center_id: 1,
      work_center_name: 'Laser 1',
      status: 'ready',
      setup_time_hours: 0.5,
      run_time_hours: 2,
    },
    {
      id: 102,
      sequence: 20,
      operation_number: 'OP20',
      name: 'Bend',
      work_center_id: 2,
      work_center_name: 'Brake 1',
      status: 'pending',
      setup_time_hours: 0.25,
      run_time_hours: 1,
    },
  ],
};

const PART = {
  id: 9,
  part_number: 'PN-0099',
  name: 'Mount Plate',
  revision: 'C',
  drawing_number: 'DWG-9',
  unit_of_measure: 'each',
};

function renderTraveler() {
  return render(
    <MemoryRouter initialEntries={['/print/traveler/42']}>
      <Routes>
        <Route path="/print/traveler/:id" element={<PrintTraveler />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('PrintTraveler scan QRs', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkOrder.mockResolvedValue(WORK_ORDER);
    mockedApi.getPart.mockResolvedValue(PART);
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-2026-0042',
      quantity_ordered: 25,
      has_bom: false,
      materials: [],
    });
    mockedToDataURL.mockImplementation(async (payload: string) => `data:image/png;base64,${encodeURIComponent(payload)}`);
  });

  it('renders ONE header QR with the job URL payload — the second WO scan QR is gone', async () => {
    renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());
    await waitFor(() =>
      expect(mockedToDataURL).toHaveBeenCalledWith('http://localhost/work-orders/42', expect.any(Object))
    );

    await waitFor(() => expect(screen.getByAltText('Work Order QR')).toBeInTheDocument());
    expect(screen.getByText('Scan for job')).toBeInTheDocument();
    // The second header QR (plain WO:{number} payload) is removed.
    expect(screen.queryByAltText('Scan code WO:WO-2026-0042')).not.toBeInTheDocument();
    expect(mockedToDataURL).not.toHaveBeenCalledWith('WO:WO-2026-0042', expect.any(Object));
    // 1 header QR + 2 op QRs — nothing else.
    expect(mockedToDataURL).toHaveBeenCalledTimes(3);

    // The human-readable WO number is still printed (header, QR caption, footer).
    expect(screen.getAllByText('WO-2026-0042').length).toBeGreaterThanOrEqual(2);
  });

  it('generates one URL QR per routing step that deep-links the shop floor to the operation', async () => {
    renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByAltText('Scan code OP:101')).toBeInTheDocument());
    expect(screen.getByAltText('Scan code OP:102')).toBeInTheDocument();

    // Plain-text OP:{id} payloads do nothing on phones — they must be URLs now.
    expect(mockedToDataURL).not.toHaveBeenCalledWith('OP:101', expect.any(Object));
    expect(mockedToDataURL).not.toHaveBeenCalledWith('OP:102', expect.any(Object));

    for (const opId of [101, 102]) {
      const payload = mockedToDataURL.mock.calls
        .map(([value]) => value as string)
        .find((value) => value.includes(`OP%3A${opId}`));
      expect(payload).toBe(
        `http://localhost/shop-floor/operations?scan=${encodeURIComponent(`OP:${opId}`)}`
      );
      // Decode and check: the scan param round-trips back to the op code.
      const url = new URL(payload!);
      expect(url.pathname).toBe('/shop-floor/operations');
      expect(url.searchParams.get('scan')).toBe(`OP:${opId}`);
      // The mono human-readable code stays printed for manual entry.
      expect(screen.getByText(`OP:${opId}`)).toBeInTheDocument();
    }
  });

  it('keeps each routing row whole across print page breaks (a split QR does not scan)', async () => {
    const { container } = renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());
    const styleText = Array.from(container.querySelectorAll('style'))
      .map((style) => style.textContent || '')
      .join('\n');
    expect(styleText).toContain('@media print');
    expect(styleText).toContain('tbody tr { break-inside: avoid; }');
  });

  it('renders the print-control footer with printed-by, part revision, and the uncontrolled stance', async () => {
    renderTraveler();

    await waitFor(() => expect(screen.getByText('UNCONTROLLED WHEN PRINTED')).toBeInTheDocument());
    expect(screen.getByText('Printed by: Quinn Printer')).toBeInTheDocument();
    expect(screen.getByText('Part Rev: C')).toBeInTheDocument();
    expect(screen.getByText(/Routing Rev: not recorded on work order/)).toBeInTheDocument();
    expect(screen.getByText(/^Printed: /)).toBeInTheDocument();
  });
});

describe('PrintTraveler — part-less standalone laser nest WO', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // A standalone laser nest WO: part_id NULL, quantity = sheet runs.
    mockedApi.getWorkOrder.mockResolvedValue({
      ...WORK_ORDER,
      part_id: null,
      work_order_type: 'laser_cutting',
    });
    mockedApi.getMaterialRequirements.mockResolvedValue({
      work_order_id: 42,
      work_order_number: 'WO-2026-0042',
      quantity_ordered: 25,
      has_bom: false,
      materials: [],
    });
    mockedToDataURL.mockImplementation(async (payload: string) => `data:image/png;base64,${encodeURIComponent(payload)}`);
  });

  it('skips the part fetch, renders an em-dash part number, and suppresses the "No BOM" note', async () => {
    renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());

    // No part on the WO → no part fetch fired.
    expect(mockedApi.getPart).not.toHaveBeenCalled();
    // The Part Number cell renders an em-dash, not a blank/undefined.
    expect(screen.getByText('—')).toBeInTheDocument();
    // has_bom:false on a part-less WO must NOT print the "No BOM defined for
    // this part" note — there is no part for it to refer to.
    expect(screen.queryByText(/no bom defined for this part/i)).not.toBeInTheDocument();
    // The traveler still renders its routing steps and header QR.
    await waitFor(() => expect(screen.getByAltText('Work Order QR')).toBeInTheDocument());
    expect(screen.getByText('OP:101')).toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });
});
