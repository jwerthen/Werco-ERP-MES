/**
 * A0.4 traveler QR plumbing — /print/traveler/:id
 *
 * Locks: the traveler renders (1) the existing phone URL QR, (2) a WO-level
 * scan QR with payload WO:{work_order_number}, (3) one per-operation scan QR
 * per routing step with payload OP:{operation_id}, and (4) the print-control
 * footer (UNCONTROLLED WHEN PRINTED, printed-at, printed-by, part revision).
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

  it('generates the WO-level scan QR and one OP QR per routing step', async () => {
    renderTraveler();

    await waitFor(() => expect(screen.getByText('WORK ORDER TRAVELER')).toBeInTheDocument());
    await waitFor(() => expect(mockedToDataURL).toHaveBeenCalledWith('WO:WO-2026-0042', expect.any(Object)));
    expect(mockedToDataURL).toHaveBeenCalledWith('OP:101', expect.any(Object));
    expect(mockedToDataURL).toHaveBeenCalledWith('OP:102', expect.any(Object));
    // The original phone URL QR is preserved alongside the scan codes.
    expect(mockedToDataURL).toHaveBeenCalledWith(expect.stringContaining('/work-orders/42'), expect.any(Object));

    await waitFor(() => expect(screen.getByAltText('Scan code WO:WO-2026-0042')).toBeInTheDocument());
    expect(screen.getByAltText('Scan code OP:101')).toBeInTheDocument();
    expect(screen.getByAltText('Scan code OP:102')).toBeInTheDocument();
    expect(screen.getByAltText('Work Order QR')).toBeInTheDocument();
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
