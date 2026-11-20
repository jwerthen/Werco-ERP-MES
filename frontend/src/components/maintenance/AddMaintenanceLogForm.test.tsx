/**
 * Filing a log entry against a maintenance work order: the body names the work order and never a
 * target, the description is trimmed, cost and date go only when given (the date read as Central
 * and sent as UTC), and a refusal keeps everything typed under the server's own words.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';
import { AddMaintenanceLogForm } from './AddMaintenanceLogForm';
import type { MaintenanceLogEntry } from '../../types/maintenance';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { createMaintenanceLog: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const saved: MaintenanceLogEntry = {
  id: 90,
  work_center_id: 3,
  asset_id: null,
  target_type: 'work_center',
  target_code: 'MILL-1',
  target_name: 'Synthetic Mill',
  maintenance_wo_id: 41,
  event_type: 'repair',
  description: 'Replaced belt',
  performed_by: 7,
  performed_by_name: 'Pat Synthetic',
  event_date: '2026-11-19T14:30:00Z',
  cost: 42.5,
  created_at: '2026-11-19T14:31:00Z',
};

const renderForm = () => {
  const onAdded = jest.fn();
  const onDirtyChange = jest.fn();
  render(
    <ToastProvider>
      <AddMaintenanceLogForm workOrderId={41} onAdded={onAdded} onDirtyChange={onDirtyChange} />
    </ToastProvider>
  );
  const form = screen.getByRole('form', { name: 'Add log entry' });
  return { form, onAdded, onDirtyChange };
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.createMaintenanceLog.mockResolvedValue(saved);
});

describe('AddMaintenanceLogForm', () => {
  it('sends the work order, no target, and a trimmed description; cost and date only when given', async () => {
    const { form, onAdded, onDirtyChange } = renderForm();
    const add = within(form).getByRole('button', { name: 'Add entry' });
    expect(add).toBeDisabled(); // nothing described yet
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: '  Belt glazed  ' } });
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(add);
    await waitFor(() => expect(mockedApi.createMaintenanceLog).toHaveBeenCalledTimes(1));
    const body = mockedApi.createMaintenanceLog.mock.calls[0][0];
    expect(body).toEqual({ maintenance_wo_id: 41, event_type: 'note', description: 'Belt glazed' });
    expect(body).not.toHaveProperty('work_center_id');
    expect(body).not.toHaveProperty('asset_id');

    // Saved: the form resets, the page is told (to re-read the timeline), and it says so.
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith(saved));
    expect(within(form).getByLabelText(/^Description/)).toHaveValue('');
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    expect(await screen.findByText('Log entry added')).toBeInTheDocument();
  });

  it('sends the chosen type, the cost, and the Central date as UTC', async () => {
    const { form } = renderForm();
    fireEvent.change(within(form).getByLabelText('Type'), { target: { value: 'part_replacement' } });
    fireEvent.change(within(form).getByLabelText('Cost ($)'), { target: { value: '42.50' } });
    const when = within(form).getByLabelText('When (Central)');
    expect(when).toHaveAccessibleDescription('Leave blank for now. At most 30 days back.');
    fireEvent.change(when, { target: { value: '2026-11-19T08:30' } });
    fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: 'Replaced belt' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add entry' }));

    await waitFor(() => expect(mockedApi.createMaintenanceLog).toHaveBeenCalledTimes(1));
    expect(mockedApi.createMaintenanceLog).toHaveBeenCalledWith({
      maintenance_wo_id: 41,
      event_type: 'part_replacement',
      description: 'Replaced belt',
      cost: 42.5,
      event_date: '2026-11-19T14:30:00.000Z',
    });
  });

  it('offers the five types a person files, and never started / completed', () => {
    const { form } = renderForm();
    const options = within(within(form).getByLabelText('Type')).getAllByRole('option');
    expect(options.map(option => option.textContent)).toEqual([
      'Inspection',
      'Repair',
      'Part replacement',
      'Observation',
      'Note',
    ]);
  });

  it('refuses a negative cost before sending', () => {
    const { form } = renderForm();
    fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: 'Checked' } });
    fireEvent.change(within(form).getByLabelText('Cost ($)'), { target: { value: '-5' } });
    expect(within(form).getByText('Cost must be zero or more.')).toBeInTheDocument();
    const add = within(form).getByRole('button', { name: 'Add entry' });
    expect(add).toBeDisabled();
    fireEvent.click(add);
    expect(mockedApi.createMaintenanceLog).not.toHaveBeenCalled();
  });

  it.each([
    [
      409,
      'A log entry on maintenance work order MWO-2026-0041 is filed against that work order’s own work center or asset.',
    ],
    [422, 'event_date can be at most 30 days back.'],
  ])('a %i shows the server’s words and keeps what was typed', async (status, detail) => {
    mockedApi.createMaintenanceLog.mockRejectedValueOnce({ response: { status, data: { detail } } });
    const { form, onAdded } = renderForm();
    fireEvent.change(within(form).getByLabelText(/^Description/), { target: { value: 'Replaced belt' } });
    fireEvent.change(within(form).getByLabelText('When (Central)'), { target: { value: '2026-09-01T08:00' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add entry' }));

    expect(await within(form).findByRole('alert')).toHaveTextContent(detail);
    expect(within(form).getByLabelText(/^Description/)).toHaveValue('Replaced belt');
    expect(within(form).getByLabelText('When (Central)')).toHaveValue('2026-09-01T08:00');
    expect(onAdded).not.toHaveBeenCalled();
    expect(within(form).getByRole('button', { name: 'Add entry' })).toBeEnabled();
  });
});
