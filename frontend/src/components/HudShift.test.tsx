/**
 * Header HUD SHIFT badge — driven by GET /production-calendar, not a hard-coded table.
 *
 * Locks: it shows the DEFAULT pattern's running shift from the server's UTC
 * occurrences; the 15:30-16:00 hand-off shows the OUTGOING shift; a gap, an
 * unconfigured calendar or a failed read all degrade to the em dash (never a crash,
 * never a blocked render); and the badge refetches when the production day ends.
 */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import HudShift from './HudShift';
import api from '../services/api';
import { unconfiguredCalendar, wercoCalendar } from '../test-utils/productionCalendarFixtures';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getProductionCalendar: jest.fn() },
}));

const mockApi = api as jest.Mocked<typeof api>;

function badge() {
  return screen.getByTestId('hud-shift');
}

afterEach(() => {
  jest.useRealTimers();
  jest.clearAllMocks();
});

function freezeAt(iso: string) {
  jest.useFakeTimers({ doNotFake: ['queueMicrotask', 'nextTick', 'setImmediate'] });
  jest.setSystemTime(new Date(iso));
}

test('renders the default pattern shift running now', async () => {
  freezeAt('2026-11-10T16:00:00Z'); // 10:00 CST
  mockApi.getProductionCalendar.mockResolvedValue(wercoCalendar());
  render(<HudShift />);
  await waitFor(() => expect(badge()).toHaveTextContent('S1'));
  expect(badge()).toHaveAttribute('title', '1st shift · 7:30 AM – 4:00 PM');
});

test('under StrictMode the first answer is applied at once and the calendar is read once', async () => {
  freezeAt('2026-11-10T16:00:00Z'); // 10:00 CST
  let resolve: (value: ReturnType<typeof wercoCalendar>) => void = () => {};
  mockApi.getProductionCalendar.mockImplementation(
    () =>
      new Promise(r => {
        resolve = r;
      })
  );
  render(
    <React.StrictMode>
      <HudShift />
    </React.StrictMode>
  );
  // The effect ran twice (mount, unmount, mount); the in-flight read is shared.
  expect(mockApi.getProductionCalendar).toHaveBeenCalledTimes(1);
  await act(async () => {
    resolve(wercoCalendar());
  });
  // No waiting for a refetch tick: the shift shows as soon as the one read answers.
  expect(badge()).toHaveTextContent('S1');
});

test('in the 15:30-16:00 hand-off it shows the OUTGOING shift', async () => {
  freezeAt('2026-11-10T21:45:00Z'); // 15:45 CST
  mockApi.getProductionCalendar.mockResolvedValue(wercoCalendar());
  render(<HudShift />);
  await waitFor(() => expect(badge()).toHaveTextContent('S1'));
  expect(badge().getAttribute('title')).toContain('hand-off with S2');
});

test('weld shifts never show: the badge follows the DEFAULT pattern', async () => {
  freezeAt('2026-11-11T07:30:00Z'); // 01:30 CST Wed: W2 runs, no rest-of-shop shift
  mockApi.getProductionCalendar.mockResolvedValue(wercoCalendar());
  render(<HudShift />);
  await waitFor(() => expect(badge()).toHaveAttribute('title', 'No active shift'));
  expect(badge()).toHaveTextContent('—');
});

test('an unconfigured calendar reads as the em dash', async () => {
  freezeAt('2026-11-10T16:00:00Z');
  mockApi.getProductionCalendar.mockResolvedValue(unconfiguredCalendar());
  render(<HudShift />);
  await waitFor(() => expect(badge()).toHaveAttribute('title', 'No production calendar configured'));
  expect(badge()).toHaveTextContent('—');
});

test('a failed read degrades to the em dash without throwing', async () => {
  freezeAt('2026-11-10T16:00:00Z');
  mockApi.getProductionCalendar.mockRejectedValue(new Error('Network Error'));
  render(<HudShift />);
  // Renders immediately, before the request settles: nothing waits on it.
  expect(badge()).toHaveTextContent('—');
  await waitFor(() => expect(badge()).toHaveAttribute('title', 'Shift unavailable'));
  expect(badge()).toHaveTextContent('—');
});

test.each([
  ['an empty object', {}],
  ['an HTML page', '<!doctype html><html></html>'],
  ['a calendar without today', { ...wercoCalendar(), today: undefined }],
])('a body that is not a calendar (%s) degrades to the em dash, never a crash', async (_label, body) => {
  // The badge renders in the header of every page: a throw here would put the whole app
  // behind the global error boundary (it did, for every Playwright spec whose catch-all
  // answers an unknown API call with `{}`).
  freezeAt('2026-11-10T16:00:00Z');
  mockApi.getProductionCalendar.mockResolvedValue(body as unknown as ReturnType<typeof wercoCalendar>);
  render(<HudShift />);
  await waitFor(() => expect(badge()).toHaveAttribute('title', 'Shift unavailable'));
  expect(badge()).toHaveTextContent('—');
  // The 30-second tick re-reads the (still absent) calendar without throwing either.
  await act(async () => {
    jest.advanceTimersByTime(30_000);
  });
  expect(badge()).toHaveTextContent('—');
});

test('refetches for the next production day when today ends', async () => {
  freezeAt('2026-11-11T10:59:00Z'); // 04:59 CST: last minute of Tuesday's production day
  const wednesday = wercoCalendar({
    today: {
      production_day: '2026-11-11',
      start_utc: '2026-11-11T11:00:00Z',
      end_utc: '2026-11-12T11:00:00Z',
      occurrences: [
        {
          shift_id: 3,
          code: 'S1',
          name: '1st shift',
          pattern_id: 2,
          production_day: '2026-11-11',
          start_utc: '2026-11-11T13:30:00Z',
          end_utc: '2026-11-11T22:00:00Z',
          planned_minutes: 480,
        },
      ],
    },
  });
  mockApi.getProductionCalendar.mockResolvedValueOnce(wercoCalendar()).mockResolvedValueOnce(wednesday);
  render(<HudShift />);
  await waitFor(() => expect(badge()).toHaveAttribute('title', 'No active shift'));
  expect(mockApi.getProductionCalendar).toHaveBeenCalledTimes(1);

  // Two minutes later (past 11:00Z) the next tick asks for the new production day...
  await act(async () => {
    jest.advanceTimersByTime(120_000);
  });
  await waitFor(() => expect(mockApi.getProductionCalendar).toHaveBeenCalledTimes(2));

  // ...and at 07:30 CST Wednesday the badge reads Wednesday's S1.
  await act(async () => {
    jest.setSystemTime(new Date('2026-11-11T13:31:00Z'));
    jest.advanceTimersByTime(30_000);
  });
  await waitFor(() => expect(badge()).toHaveTextContent('S1'));
});
