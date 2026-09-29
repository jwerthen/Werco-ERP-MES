import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ShopFloorSimple from './ShopFloorSimple';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(), getWorkCenterQueue: jest.fn(), getWorkCenters: jest.fn(),
    getDashboard: jest.fn(), getMyActiveJob: jest.fn(), getScrapReasonCodes: jest.fn(),
  },
}));
jest.mock('../hooks/usePermissions', () => ({ usePermissions: () => ({ can: () => false }) }));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, company_id: 1, role: 'operator' } }) }));
jest.mock('../context/CompanyContext', () => ({ useCompany: () => ({ currentCompany: { id: 1 } }) }));
jest.mock('../hooks/usePhoneLayout', () => ({ usePhoneLayout: () => true }));
jest.mock('../components/shopfloor/ShopFloorCameraScanner', () => ({ __esModule: true, default: () => null }));
jest.mock('../components/kiosk/KioskDocViewer', () => ({ __esModule: true, default: () => null }));

const mockedApi = api as jest.Mocked<typeof api>;
const centers = [
  { id: 1, code: 'FAB-02', name: 'Fabrication' },
  { id: 2, code: 'LAS-01', name: 'Laser' },
  { id: 3, code: 'BRK-01', name: 'Brake' },
];
const count = (id: number, queued: number, active = 0) => ({ id, queued_operations: queued, active_operations: active });
const workspaceKey = 'shop_floor_workspace:v1:company:1:user:1';

function renderPage(path = '/shop-floor/operations') {
  return render(<MemoryRouter initialEntries={[path]}><ShopFloorSimple /></MemoryRouter>);
}

async function getStationPicker() {
  fireEvent.click(await screen.findByRole('button', { name: 'All operations' }));
  return screen.findByLabelText('Work center');
}

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  mockedApi.getWorkCenters.mockResolvedValue(centers as never);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [count(1, 0), count(2, 5), count(3, 2)] });
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
});

it('replaces an empty automatic station with the station having the most live queued work before loading operations', async () => {
  localStorage.setItem('shop_floor_work_center_id', '1');
  renderPage();
  expect(await getStationPicker()).toHaveValue('2');
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenCalledWith({ work_center_id: 2 }));
  expect(mockedApi.getShopFloorOperations).not.toHaveBeenCalledWith({ work_center_id: 1 });
});

it('breaks equal queue counts alphabetically by station code, independent of API order', async () => {
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [count(1, 0), count(2, 5), count(3, 5)] });
  renderPage();
  expect(await getStationPicker()).toHaveValue('3');
});

it('recalculates an automatic default on return when that station becomes idle', async () => {
  localStorage.setItem('shop_floor_work_center_id', '1');
  const firstVisit = renderPage();
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({ work_center_id: 2 }));
  firstVisit.unmount();
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [count(1, 0), count(2, 0), count(3, 2)] });
  renderPage();
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({ work_center_id: 3 }));
});

it('keeps a current default with active work even when another center has more queued work', async () => {
  localStorage.setItem('shop_floor_work_center_id', '1');
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [count(1, 0, 1), count(2, 5)] });
  renderPage();
  expect(await getStationPicker()).toHaveValue('1');
});

it('selects active-only work when there are no queued operations', async () => {
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [count(1, 0), count(2, 0, 1)] });
  renderPage();
  expect(await getStationPicker()).toHaveValue('2');
});

it('lets the operator choose an empty station and keeps it across refresh', async () => {
  renderPage();
  fireEvent.change(await getStationPicker(), { target: { value: '1' } });
  await waitFor(() => expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({ work_center_id: 1 }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh jobs' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh jobs' })).toBeEnabled());
  expect(screen.getByLabelText('Work center')).toHaveValue('1');
});

it.each([1, null])('preserves a saved operator selection of %s even when work exists elsewhere', async workCenterId => {
  sessionStorage.setItem(workspaceKey, JSON.stringify({ workCenterId, selectedOperationId: null, activeTimeEntryId: null, view: 'all', savedAt: Date.now() }));
  renderPage();
  expect(await getStationPicker()).toHaveValue(workCenterId ? String(workCenterId) : '');
});

it.each(['?work_center_id=1', '?work_center_code=FAB-02', '?dept=fabrication', '?kiosk=1'])(
  'preserves an explicitly assigned station with %s', async search => {
    localStorage.setItem('shop_floor_work_center_id', '1');
    renderPage(`/shop-floor/operations${search}`);
    expect(await getStationPicker()).toHaveValue('1');
  }
);

it('retains the empty state when the whole shop is idle', async () => {
  localStorage.setItem('shop_floor_work_center_id', '1');
  mockedApi.getDashboard.mockResolvedValue({ work_centers: centers.map(center => count(center.id, 0)) });
  renderPage();
  expect(await getStationPicker()).toHaveValue('1');
  fireEvent.click(screen.getByRole('button', { name: 'All operations' }));
  expect(await screen.findByText('No operations found for Fabrication')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'View All Operations' })).toBeInTheDocument();
});
