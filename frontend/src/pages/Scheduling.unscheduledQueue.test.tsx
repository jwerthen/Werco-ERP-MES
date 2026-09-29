import React from 'react';
import { fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../components/ui/Toast';
import api from '../services/api';
import Scheduling from './Scheduling';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getWorkCenters: jest.fn(), getSchedulableWorkOrders: jest.fn(), getCapacityHeatmap: jest.fn() },
}));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }) }));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({ getAccessToken: () => 'test', buildWsUrl: () => 'ws://localhost/test' }));

const mockedApi = api as jest.Mocked<typeof api>;
const jobs = [1, 2, 3, 4].map(id => ({
  id, work_order_id: id, work_order_number: `WO-${id}`, current_operation_id: id * 10,
  current_operation_name: 'Cut', current_operation_number: '10', current_operation_sequence: 10,
  part_number: `PART-${id}`, part_name: 'Bracket', work_center_id: id === 2 ? 8 : 7,
  status: id === 4 ? 'complete' : 'released', operation_status: 'pending', quantity: 10,
  quantity_complete: 0, priority: 5, total_operations: 1, operations_complete: 0,
  remaining_hours: 8, setup_hours: 0, run_hours: 8, scheduled_start: id === 3 ? '2099-01-01' : null,
}));

function renderPage(path = '/scheduling') {
  return render(<MemoryRouter initialEntries={[path]}><ToastProvider><Scheduling /></ToastProvider></MemoryRouter>);
}

beforeEach(() => {
  jest.clearAllMocks();
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
  mockedApi.getWorkCenters.mockResolvedValue([
    { id: 7, code: 'LAS-1', name: 'Laser', capacity_hours_per_day: 8 },
    { id: 8, code: 'BRK-1', name: 'Brake', capacity_hours_per_day: 8 },
  ] as never);
  mockedApi.getSchedulableWorkOrders.mockResolvedValue(jobs as never);
  mockedApi.getCapacityHeatmap.mockResolvedValue({ work_centers: [], overloaded_work_centers: [] } as never);
});

it('offers a primary first-paint path to every unscheduled job and retains accurate KPIs', async () => {
  renderPage('/scheduling?work_center=7');
  const action = await screen.findByRole('button', { name: 'View unscheduled jobs (2)' });
  expect(screen.getByRole('heading', { name: '2 jobs await scheduling' })).toBeInTheDocument();
  const unscheduledStat = screen.getByText('Unscheduled', { selector: 'p' }).closest('.card')!;
  const scheduledStat = screen.getByText('Scheduled', { selector: 'p' }).closest('.card')!;
  expect(within(unscheduledStat as HTMLElement).getByText('2')).toBeInTheDocument();
  expect(within(scheduledStat as HTMLElement).getByText('1')).toBeInTheDocument();

  fireEvent.change(screen.getByRole('textbox', { name: 'Search WO#, part' }), { target: { value: 'missing' } });
  fireEvent.click(action);

  const queue = screen.getByLabelText('Dispatch Queue');
  expect(queue).toHaveFocus();
  expect(queue.scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
  expect(screen.getByLabelText('Show scheduled rows')).not.toBeChecked();
  expect(screen.getByLabelText('Filter by work center')).toHaveValue('');
  expect(screen.getByRole('textbox', { name: 'Search WO#, part' })).toHaveValue('');
  expect(within(queue).getByText('WO-1')).toBeInTheDocument();
  expect(within(queue).getByText('WO-2')).toBeInTheDocument();
  expect(within(queue).queryByText('WO-3')).not.toBeInTheDocument();
  expect(within(queue).queryByText('WO-4')).not.toBeInTheDocument();
  expect(within(unscheduledStat as HTMLElement).getByText('2')).toBeInTheDocument();
  expect(within(scheduledStat as HTMLElement).getByText('1')).toBeInTheDocument();
});

it('does not show the unscheduled callout when all open work has a start date', async () => {
  mockedApi.getSchedulableWorkOrders.mockResolvedValue(jobs.map(job => ({ ...job, scheduled_start: '2099-01-01' })) as never);
  renderPage();
  await screen.findByRole('heading', { name: 'Production Schedule' });
  expect(screen.queryByRole('button', { name: /View unscheduled jobs/ })).not.toBeInTheDocument();
  const unscheduledStat = screen.getByText('Unscheduled', { selector: 'p' }).closest('.card')!;
  expect(within(unscheduledStat as HTMLElement).getByText('0')).toBeInTheDocument();
});


it('opens an initially overloaded capacity panel and respects a later manual collapse on refresh', async () => {
  mockedApi.getCapacityHeatmap.mockResolvedValue({ overloaded_work_centers: [7], work_centers: [{
    work_center_id: 7, work_center_code: 'LAS-1', work_center_name: 'Laser',
    days: [{ date: '2026-09-28', scheduled_hours: 12, capacity_hours: 8, overloaded: true, utilization_pct: 150 }],
  }] } as never);
  renderPage();
  const capacity = await screen.findByRole('button', { name: /Machine Capacity/ });
  expect(capacity).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText('1 overloaded day')).toBeInTheDocument();
  fireEvent.click(capacity);
  expect(capacity).toHaveAttribute('aria-expanded', 'false');
  const requests = mockedApi.getCapacityHeatmap.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'Today' }));
  await waitFor(() => expect(mockedApi.getCapacityHeatmap.mock.calls.length).toBeGreaterThan(requests));
  expect(capacity).toHaveAttribute('aria-expanded', 'false');
});

it('leaves capacity collapsed when initial data has no overload', async () => {
  renderPage();
  expect(await screen.findByRole('button', { name: /Machine Capacity/ })).toHaveAttribute('aria-expanded', 'false');
});
