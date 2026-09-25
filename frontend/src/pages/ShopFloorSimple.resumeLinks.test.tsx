import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import { WorkCenter } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    resolveScanAction: jest.fn(),
    scannerLookup: jest.fn(),
  },
}));
jest.mock('../hooks/usePermissions', () => ({ usePermissions: () => ({ can: () => false }) }));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }) }));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: 1 } }) }));
jest.mock('../components/shopfloor/ShopFloorCameraScanner', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/kiosk/KioskDocViewer', () => ({ __esModule: true, default: () => null }));

const mockedApi = api as jest.Mocked<typeof api>;
const savedWorkspaceKey = 'shop_floor_workspace:v1:company:1:user:1';
const originalMatchMedia = window.matchMedia;
const laser: WorkCenter = {
  id: 1,
  version: 1,
  code: 'LASER',
  name: 'Laser',
  work_center_type: 'laser',
  hourly_rate: 95,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};
const brake: WorkCenter = { ...laser, id: 2, code: 'BRAKE1', name: 'Brake Forming' };

function saveWorkspace(workCenterId: number | null) {
  sessionStorage.setItem(
    savedWorkspaceKey,
    JSON.stringify({
      selectedOperationId: null,
      activeTimeEntryId: null,
      view: 'all',
      workCenterId,
      savedAt: Date.now(),
    })
  );
}

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>;
}

function renderPage(path = '/shop-floor/operations') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ShopFloorSimple />
      <LocationProbe />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  window.matchMedia = jest.fn(query => ({
    matches: query === '(max-width: 767px)',
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  }));
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.getWorkCenters.mockResolvedValue([laser, brake]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
});
afterEach(() => {
  window.matchMedia = originalMatchMedia;
});

it.each(['work_center_id=2', 'work_center_code=BRAKE1', 'dept=forming'])(
  'honors explicit %s before the saved operator work center',
  async query => {
    saveWorkspace(1);
    localStorage.setItem('shop_floor_work_center_id', '1');
    renderPage(`/shop-floor/operations?${query}`);
    expect(await screen.findByLabelText('Work center')).toHaveValue('2');
    await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({ work_center_id: 2 }));
    expect(JSON.parse(sessionStorage.getItem(savedWorkspaceKey)!)).toMatchObject({ workCenterId: 2 });
  }
);

it('validates remembered work centers and drops one that is no longer available', async () => {
  saveWorkspace(999);
  localStorage.setItem('shop_floor_work_center_id', '1');
  renderPage();
  expect(await screen.findByLabelText('Work center')).toHaveValue('');
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({}));
  expect(JSON.parse(sessionStorage.getItem(savedWorkspaceKey)!)).toMatchObject({ workCenterId: null });
});

it('keeps a saved All selection and does not restore the legacy workstation over it', async () => {
  saveWorkspace(null);
  localStorage.setItem('shop_floor_work_center_id', '1');
  renderPage();
  expect(await screen.findByLabelText('Work center')).toHaveValue('');
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({}));
});

it('allows manual station changes after opening a station link and retains them on refresh', async () => {
  saveWorkspace(1);
  renderPage('/shop-floor/operations?work_center_id=2');
  fireEvent.change(await screen.findByLabelText('Work center'), { target: { value: '1' } });
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({ work_center_id: 1 }));
  const beforeRefresh = mockedApi.getShopFloorOperations.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh jobs' }));
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenCalledTimes(beforeRefresh + 1));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh jobs' })).toBeEnabled());
  expect(screen.getByLabelText('Work center')).toHaveValue('1');
});

it('keeps a failed phone deep-link scan recoverable and removes it only after a successful reload', async () => {
  const path = '/shop-floor/operations?kiosk=1&scan=WO%3AWO-42';
  mockedApi.resolveScanAction.mockRejectedValue(new Error('Offline'));
  mockedApi.scannerLookup.mockRejectedValue(new Error('Offline'));
  const failed = renderPage(path);
  expect(await screen.findByRole('alert')).toHaveTextContent('Your code is kept in this link');
  expect(screen.getByTestId('location-search')).toHaveTextContent('scan=WO%3AWO-42');
  expect(mockedApi.resolveScanAction).toHaveBeenCalledTimes(1);
  expect(mockedApi.scannerLookup).toHaveBeenCalledTimes(1);
  failed.unmount();

  mockedApi.resolveScanAction.mockResolvedValue({
    kind: 'work_order',
    code: 'WO:WO-42',
    operations: [],
    work_order: {
      id: 42,
      work_order_number: 'WO-42',
      status: 'released',
      quantity_ordered: 20,
      quantity_complete: 0,
      part_number: 'PART-42',
      part_name: 'Mount plate',
      current_operation_id: 101,
    },
  });
  renderPage(path);
  await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent(/^\?kiosk=1$/));
  expect(mockedApi.resolveScanAction).toHaveBeenCalledTimes(2);
  expect(mockedApi.scannerLookup).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('searchbox', { name: 'Search work orders or parts' })).toHaveValue('WO-42');
});
