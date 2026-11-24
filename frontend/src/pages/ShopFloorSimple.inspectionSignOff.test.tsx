/**
 * ShopFloorSimple — the in-process inspection chip and Sign off on the operation cards.
 *
 * `GET /shop-floor/operations` rows carry `requires_inspection` and
 * `inspection_complete`; an inspection point shows "Inspection required" until the
 * audited sign-off is recorded. Sign off is offered to the route's roles only
 * (ADMIN / MANAGER / SUPERVISOR / QUALITY) and is NON-optimistic: the card flips from
 * the re-read list.
 *
 * Harness mirrors ShopFloorSimple.clearHold.test.tsx.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import { ToastProvider } from '../components/ui';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    signOffOperationInspection: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

let mockRole = 'supervisor';

jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, company_id: 1, role: mockRole, is_superuser: false } }),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: { id: 1 } }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const op = (inspectionComplete: boolean) => ({
  id: 303,
  work_order_id: 42,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: '30',
  operation_name: 'Deburr',
  description: null,
  work_center_id: 1,
  work_center_name: 'Bench 1',
  status: 'in_progress',
  quantity_ordered: 25,
  quantity_complete: 5,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: true,
  inspection_complete: inspectionComplete,
});

function renderShopFloor() {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/shop-floor/operations']}>
        <Routes>
          <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  mockRole = 'supervisor';
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [op(false)] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
});

describe('ShopFloorSimple inspection sign-off', () => {
  it('shows the chip only, no Sign off, to an operator', async () => {
    mockRole = 'operator';
    renderShopFloor();
    const chip = await screen.findByTestId('shop-floor-inspection-303');
    expect(within(chip).getByText('inspection required')).toBeInTheDocument();
    expect(within(chip).queryByRole('button')).not.toBeInTheDocument();
  });

  it('signs off and re-reads the list before showing Inspected', async () => {
    mockedApi.signOffOperationInspection.mockResolvedValue({
      message: 'Operation inspection recorded',
      operation_id: 303,
      work_order_id: 42,
      requires_inspection: true,
      inspection_complete: true,
      inspection_type: 'in_process',
      signed_off_by_id: 1,
      signed_off_by_name: 'Robin Vale',
      signed_off_at: '2026-11-23T15:04:05Z',
    });
    renderShopFloor();
    const chip = await screen.findByTestId('shop-floor-inspection-303');

    fireEvent.click(within(chip).getByRole('button', { name: /Sign off inspection for WO-2026-0042/ }));
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [op(true)] });
    const loadsBefore = mockedApi.getShopFloorOperations.mock.calls.length;
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Sign off' }));

    await waitFor(() => expect(mockedApi.getShopFloorOperations.mock.calls.length).toBeGreaterThan(loadsBefore));
    expect(mockedApi.signOffOperationInspection).toHaveBeenCalledWith(303, { inspection_type: 'in_process' });
    await waitFor(() =>
      expect(within(screen.getByTestId('shop-floor-inspection-303')).getByText('inspected')).toBeInTheDocument()
    );
    expect(screen.queryByRole('button', { name: /Sign off inspection/ })).not.toBeInTheDocument();
  });
});
