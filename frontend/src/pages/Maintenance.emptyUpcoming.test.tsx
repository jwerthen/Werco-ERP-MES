import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import api from '../services/api';
import Maintenance from './Maintenance';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaintenanceDashboard: jest.fn(),
    getMaintenanceSchedules: jest.fn(),
    getMaintenanceWorkOrders: jest.fn(),
    getMaintenanceAssets: jest.fn(),
    getWorkCenters: jest.fn(),
    createMaintenanceSchedule: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));
const mockedApi = api as jest.Mocked<typeof api>;

const DASHBOARD = {
  overdue_count: 0,
  due_this_week: 0,
  completed_this_month: 0,
  completion_rate: 0,
  total_cost_month: 0,
  in_progress: 0,
  work_center_metrics: [],
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getMaintenanceDashboard.mockResolvedValue(DASHBOARD);
  mockedApi.getMaintenanceSchedules.mockResolvedValue([]);
  mockedApi.getMaintenanceWorkOrders.mockResolvedValue([]);
  mockedApi.getMaintenanceAssets.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, code: 'LAS-01', name: 'Laser' }] as any);
  mockedApi.createMaintenanceSchedule.mockResolvedValue({});
});

it.each([
  ['New Schedule', 'New Maintenance Schedule'],
  ['New Work Order', 'New Maintenance Work Order'],
])('opens the existing %s form from the Upcoming empty panel', async (action, heading) => {
  render(
    <MemoryRouter>
      <Maintenance />
    </MemoryRouter>
  );
  await screen.findAllByText('No schedules configured');
  fireEvent.click(screen.getByRole('button', { name: /^dashboard$/i }));
  const empty = (await screen.findByText('No upcoming maintenance')).closest(
    '[data-testid="empty-state"]'
  ) as HTMLElement;
  fireEvent.click(within(empty).getByRole('button', { name: action }));
  expect(within(await screen.findByRole('dialog')).getByRole('heading', { name: heading })).toBeInTheDocument();
});

it('defaults to Schedules once and preserves a later Dashboard choice after saving', async () => {
  render(
    <MemoryRouter>
      <Maintenance />
    </MemoryRouter>
  );
  await screen.findAllByText('No schedules configured');
  fireEvent.click(screen.getByRole('button', { name: /^dashboard$/i }));
  const empty = screen.getByText('No upcoming maintenance').closest('[data-testid="empty-state"]') as HTMLElement;
  fireEvent.click(within(empty).getByRole('button', { name: 'New Schedule' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'Laser optics PM' } });
  const picker = within(dialog).getByRole('combobox', { name: /^Work Center/ });
  await waitFor(() => expect(picker).toBeEnabled());
  fireEvent.click(picker);
  fireEvent.click(await screen.findByRole('option', { name: /LAS-01 Laser/ }));
  fireEvent.change(within(dialog).getByLabelText(/^Description/), { target: { value: 'Inspect optics' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
  await waitFor(() => expect(mockedApi.getMaintenanceDashboard).toHaveBeenCalledTimes(2));
  expect(await screen.findByText('No upcoming maintenance')).toBeInTheDocument();
  expect(screen.queryByText('No schedules configured')).not.toBeInTheDocument();
});

/*
 * The page follows `?tab=` after mount: a notification link (`maintenance.due` / `.overdue`) clicked
 * while the page is already open changes only the query string, and must still switch the tab. A
 * tab click sets the state and the URL together, and must never be pulled back in between.
 */
describe('Maintenance — the tab follows the URL', () => {
  /** Stands in for an in-app link (the notification bell) and shows where the router is. */
  function InAppLinks() {
    const navigate = useNavigate();
    const location = useLocation();
    return (
      <div>
        <button type="button" onClick={() => navigate('/maintenance?tab=work_orders')}>
          Link to maintenance work orders
        </button>
        <button type="button" onClick={() => navigate('/maintenance?tab=schedules')}>
          Link to maintenance schedules
        </button>
        <output data-testid="location">{`${location.pathname}${location.search}`}</output>
      </div>
    );
  }

  const renderAt = (path: string) =>
    render(
      <MemoryRouter initialEntries={[path]}>
        <InAppLinks />
        <Maintenance />
      </MemoryRouter>
    );
  const sections = () => within(screen.getByRole('navigation', { name: 'Maintenance sections' }));
  const currentTab = () =>
    sections()
      .getAllByRole('button')
      .filter(button => button.getAttribute('aria-current') === 'page')
      .map(button => button.textContent);

  it('switches tab when a link changes only ?tab= on the open page', async () => {
    renderAt('/maintenance?tab=dashboard');
    expect(await screen.findByText('No upcoming maintenance')).toBeInTheDocument();
    expect(currentTab()).toEqual(['Dashboard']);

    fireEvent.click(screen.getByRole('button', { name: 'Link to maintenance work orders' }));
    await waitFor(() => expect(currentTab()).toEqual(['Work Orders']));
    expect((await screen.findAllByText('No work orders found')).length).toBeGreaterThan(0);
    expect(screen.queryByText('No upcoming maintenance')).not.toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/maintenance?tab=work_orders');

    fireEvent.click(screen.getByRole('button', { name: 'Link to maintenance schedules' }));
    await waitFor(() => expect(currentTab()).toEqual(['Schedules']));
    expect((await screen.findAllByText('No schedules configured')).length).toBeGreaterThan(0);
    expect(screen.getByTestId('location')).toHaveTextContent('/maintenance?tab=schedules');
  });

  it('keeps a clicked tab, never flicking back to the one the URL held before', async () => {
    renderAt('/maintenance?tab=dashboard');
    expect(await screen.findByText('No upcoming maintenance')).toBeInTheDocument();
    const dashboardTab = sections().getByRole('button', { name: 'Dashboard' });

    // Every value the Dashboard tab's aria-current takes from the click on.
    const seen: Array<string | null> = [];
    const observer = new MutationObserver(() => seen.push(dashboardTab.getAttribute('aria-current')));
    observer.observe(dashboardTab, { attributes: true, attributeFilter: ['aria-current'] });
    try {
      fireEvent.click(sections().getByRole('button', { name: 'Work Orders' }));
      await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/maintenance?tab=work_orders'));
      expect((await screen.findAllByText('No work orders found')).length).toBeGreaterThan(0);
      // Let any pending effect run, then check nothing pulled the page back.
      await waitFor(() => expect(currentTab()).toEqual(['Work Orders']));
      expect(seen).not.toContain('page');
      expect(screen.queryByText('No upcoming maintenance')).not.toBeInTheDocument();
    } finally {
      observer.disconnect();
    }
  });
});
