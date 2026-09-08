import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import WorkingCalendarEditor from './WorkingCalendarEditor';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getWorkingCalendar: jest.fn(), updateWorkingCalendar: jest.fn() },
}));
const center = { id: 7, code: 'WELD-1', name: 'Welding' };
const calendar = { work_center_id: 7, version: 0, weekly_hours: [8, 8, 8, 8, 8, 8, 8], overrides: [] };
beforeEach(() => {
  jest.clearAllMocks();
  (api.getWorkingCalendar as jest.Mock).mockResolvedValue(calendar);
});

test('failed reads offer retry and cannot silently save default hours', async () => {
  (api.getWorkingCalendar as jest.Mock).mockRejectedValueOnce(new Error('offline'));
  render(<WorkingCalendarEditor center={center} onClose={jest.fn()} onSaved={jest.fn()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load');
  expect(screen.getByRole('button', { name: 'Save working calendar' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Reload current calendar' }));
  expect(await screen.findByRole('spinbutton', { name: 'Monday' })).toHaveValue(8);
  expect(api.updateWorkingCalendar).not.toHaveBeenCalled();
});

test('saves reviewed weekly shifts and shutdown with the exact version, guarding duplicate pending submission', async () => {
  let resolve!: (value: unknown) => void;
  (api.updateWorkingCalendar as jest.Mock).mockImplementation(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  const onSaved = jest.fn();
  const onClose = jest.fn();
  render(<WorkingCalendarEditor center={center} onClose={onClose} onSaved={onSaved} />);
  await screen.findByRole('spinbutton', { name: 'Monday' });
  fireEvent.click(screen.getByRole('button', { name: 'Use Monday–Friday, 8 hours' }));
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Friday' }), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add date override' }));
  fireEvent.change(screen.getByLabelText('Override 1 date'), { target: { value: '2026-09-14' } });
  fireEvent.change(screen.getByLabelText('Override 1 reason'), { target: { value: 'Plant shutdown' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save working calendar' }));
  fireEvent.click(screen.getByRole('button', { name: 'Saving calendar…' }));
  expect(api.updateWorkingCalendar).toHaveBeenCalledTimes(1);
  expect(api.updateWorkingCalendar).toHaveBeenCalledWith(7, {
    expected_version: 0,
    weekly_hours: [8, 8, 8, 8, 4, 0, 0],
    overrides: [{ date: '2026-09-14', hours: 0, reason: 'Plant shutdown' }],
  });
  expect(onClose).not.toHaveBeenCalled();
  await act(async () => {
    resolve({ ...calendar, version: 1 });
  });
  expect(onSaved).toHaveBeenCalledTimes(1);
  expect(onClose).toHaveBeenCalledTimes(1);
});

test('concurrent calendar edits retain input and require an explicit current-version reload', async () => {
  (api.updateWorkingCalendar as jest.Mock).mockRejectedValue({ response: { status: 409 } });
  render(<WorkingCalendarEditor center={center} onClose={jest.fn()} onSaved={jest.fn()} />);
  fireEvent.change(await screen.findByRole('spinbutton', { name: 'Monday' }), { target: { value: '12' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save working calendar' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('changed while you were editing'));
  expect(screen.getByRole('spinbutton', { name: 'Monday' })).toHaveValue(12);
  expect(screen.getByRole('button', { name: 'Save working calendar' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Reload current calendar' })).toBeEnabled();
});
