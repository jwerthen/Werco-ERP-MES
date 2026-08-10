/**
 * Calibration — `?filter=` URL-param handling.
 *
 * Context: the `calibration.due` cron (a wired daily 7:00 AM job, two dispatches
 * per equipment window) used to emit `/calibration/<equipment_id>`, which is not
 * a route — every one of those notifications rendered the app's 404 screen.
 *
 * It now emits the BARE `/calibration` (`notification_links.CALIBRATION_LIST`),
 * deliberately NOT `?filter=due`: the cron detects due-soon from
 * `next_calibration_date`, while `GET /calibration/equipment?status=due` filters
 * on the persisted `status` column before `update_equipment_status` recomputes
 * it — so the very rows that trigger the notification are the rows that filter
 * can exclude. A filtered landing that silently shows nothing is worse than the
 * 404 it replaced. Read the comment above `CALIBRATION_LIST` before "improving"
 * this by adding the param back.
 *
 * `?filter=` is therefore no longer a notification landing, but it is still the
 * page's own URL-backed filter state (the <select> writes it, and it survives
 * reload), so it still has to work — and the effect that syncs it is still
 * required. That is what this file covers.
 *
 * The regression these tests exist for: `statusFilter` was a `useState` lazy
 * initializer, read only at mount, so any arrival at a new query string while
 * the page was ALREADY mounted was a silent no-op. The "while mounted" cases
 * pin the effect that replaced it.
 *
 * Also pinned: the param vocabulary must cover ALL FOUR values the page's own
 * <select> writes. A narrower allowlist would normalize `active` /
 * `out_of_service` to "" and instantly revert the user's own selection.
 *
 * And the landing is honest either way: `statusFilter` drives a SERVER query
 * (`api.getEquipment(statusFilter)`), not a filter over a possibly-incomplete
 * client array. That is asserted below, not assumed.
 */

import React from 'react';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom';
import api from '../services/api';
import Calibration from './Calibration';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getEquipment: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const equipment = [
  {
    id: 1,
    equipment_id: 'CAL-001',
    name: 'Due Micrometer',
    calibration_interval_days: 365,
    status: 'due',
    is_active: true,
  },
];

let navigateTo: (to: string) => void = () => {
  throw new Error('router not mounted');
};

function NavProbe() {
  navigateTo = useNavigate();
  return null;
}

const renderAt = (url: string) =>
  render(
    <MemoryRouter initialEntries={[url]}>
      <NavProbe />
      <Routes>
        <Route path="/calibration" element={<Calibration />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getEquipment.mockResolvedValue(equipment as any);
});

const statusSelect = () => screen.getByRole('combobox') as HTMLSelectElement;

describe('?filter= on arrival', () => {
  test('?filter=due re-queries the SERVER for the due-soon set', async () => {
    renderAt('/calibration?filter=due');
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith('due'));
    expect(statusSelect().value).toBe('due');
  });

  test.each(['active', 'due', 'overdue', 'out_of_service'])(
    '?filter=%s is honored (the full <select> vocabulary, not just due/overdue)',
    async value => {
      renderAt(`/calibration?filter=${value}`);
      await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(value));
      expect(statusSelect().value).toBe(value);
    },
  );

  test('an unrecognized filter value falls back to no filter', async () => {
    renderAt('/calibration?filter=not-a-status');
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));
    expect(statusSelect().value).toBe('');
  });

  test('no param means no filter', async () => {
    renderAt('/calibration');
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));
    expect(statusSelect().value).toBe('');
  });
});

describe('?filter= changing while the page stays mounted', () => {
  test('a bell click to ?filter=due while on /calibration re-filters', async () => {
    // THE stale-initializer regression. Without the effect this asserts nothing
    // happens at all and the operator sees the unfiltered list.
    renderAt('/calibration');
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));

    act(() => navigateTo('/calibration?filter=due'));

    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith('due'));
    expect(statusSelect().value).toBe('due');
  });

  test('navigating to the SAME filter does not trigger a redundant re-fetch', async () => {
    renderAt('/calibration?filter=due');
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith('due'));
    const callsAfterLanding = mockedApi.getEquipment.mock.calls.length;

    act(() => navigateTo('/calibration?filter=due'));
    await new Promise(resolve => setTimeout(resolve, 20));

    expect(mockedApi.getEquipment.mock.calls.length).toBe(callsAfterLanding);
  });
});

describe('the URL-sync effect does not fight the user', () => {
  test('choosing a status in the <select> sticks', async () => {
    // The failure mode a naive sync effect creates: the select writes both local
    // state and the URL, then the effect re-derives from a stale URL and snaps
    // the choice back one render later.
    renderAt('/calibration');
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));

    fireEvent.change(statusSelect(), { target: { value: 'out_of_service' } });

    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith('out_of_service'));
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(statusSelect().value).toBe('out_of_service');
  });
});
