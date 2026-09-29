import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Maintenance from './Maintenance';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getMaintenanceDashboard: jest.fn(), getMaintenanceSchedules: jest.fn(), getMaintenanceWorkOrders: jest.fn(), getWorkCenters: jest.fn(), createMaintenanceSchedule: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getMaintenanceDashboard.mockResolvedValue({ upcoming: [], recent_work_orders: [] });
  mockedApi.getMaintenanceSchedules.mockResolvedValue([]);
  mockedApi.getMaintenanceWorkOrders.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, code: 'LAS-01', name: 'Laser' }] as any);
  mockedApi.createMaintenanceSchedule.mockResolvedValue({});
});

it.each([
  ['New Schedule', 'New Maintenance Schedule'],
  ['New Work Order', 'New Maintenance Work Order'],
])('opens the existing %s form from the Upcoming empty panel', async (action, heading) => {
  render(<MemoryRouter><Maintenance /></MemoryRouter>);
  await screen.findAllByText('No schedules configured');
  fireEvent.click(screen.getByRole('button', { name: /^dashboard$/i }));
  const empty = (await screen.findByText('No upcoming maintenance')).closest('[data-testid="empty-state"]') as HTMLElement;
  fireEvent.click(within(empty).getByRole('button', { name: action }));
  expect(within(await screen.findByRole('dialog')).getByRole('heading', { name: heading })).toBeInTheDocument();
});

it('defaults to Schedules once and preserves a later Dashboard choice after saving', async () => {
  render(<MemoryRouter><Maintenance /></MemoryRouter>);
  await screen.findAllByText('No schedules configured');
  fireEvent.click(screen.getByRole('button', { name: /^dashboard$/i }));
  const empty = screen.getByText('No upcoming maintenance').closest('[data-testid="empty-state"]') as HTMLElement;
  fireEvent.click(within(empty).getByRole('button', { name: 'New Schedule' }));
  const dialog = await screen.findByRole('dialog');
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
