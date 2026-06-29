/**
 * Calibration — "instrument-panel cockpit" overhaul regression.
 *
 * The page's KPI header was replaced with a 3-up clickable-filter MiniStat strip
 * (Overdue / Due Soon / Current). Each tile renders its status count and acts as
 * a filter toggle: clicking re-loads the equipment list scoped to that status
 * (the list is server-filtered — `loadEquipment` re-calls
 * `api.getEquipment(statusFilter)` whenever the filter changes), marks the tile
 * active (aria-pressed), and clicking the active tile again clears the filter.
 *
 * This guards that behavior: the strip renders with correct counts and the
 * tiles drive the status filter / re-fetch.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Calibration from './Calibration';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getEquipment: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

// One row per status the strip aggregates, plus an out_of_service row that
// the strip ignores — so the three counts are 1 / 1 / 1 and not just N/N/N.
const equipment = [
  {
    id: 1,
    equipment_id: 'CAL-001',
    name: 'Overdue Caliper',
    calibration_interval_days: 365,
    status: 'overdue',
    is_active: true,
  },
  {
    id: 2,
    equipment_id: 'CAL-002',
    name: 'Due Micrometer',
    calibration_interval_days: 365,
    status: 'due',
    is_active: true,
  },
  {
    id: 3,
    equipment_id: 'CAL-003',
    name: 'Current Height Gauge',
    calibration_interval_days: 365,
    status: 'active',
    is_active: true,
  },
  {
    id: 4,
    equipment_id: 'CAL-004',
    name: 'Shelved Indicator',
    calibration_interval_days: 365,
    status: 'out_of_service',
    is_active: false,
  },
];

function renderPage(initialEntries = ['/calibration']) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Calibration />
    </MemoryRouter>
  );
}

/** Resolve a filter MiniStat tile (a <button>) by its visible label. */
function tile(label: string | RegExp): HTMLElement {
  return screen.getByRole('button', { name: label });
}

describe('Calibration cockpit: clickable-filter MiniStat strip', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getEquipment.mockResolvedValue(equipment as any);
  });

  it('renders the 3-up Overdue / Due / Current strip with status counts', async () => {
    renderPage();

    // Wait for the initial data load to settle (loading spinner -> content).
    await screen.findByRole('heading', { name: 'Calibration Tracking' });

    const overdue = await screen.findByRole('button', { name: /Overdue/ });
    const due = tile(/Due Soon/);
    const current = tile(/Current/);

    // Each tile shows the count of equipment in that status (1 / 1 / 1; the
    // out_of_service row is excluded from all three).
    expect(within(overdue).getByText('1')).toBeInTheDocument();
    expect(within(due).getByText('1')).toBeInTheDocument();
    expect(within(current).getByText('1')).toBeInTheDocument();

    // Nothing is filtered on first load.
    expect(overdue).toHaveAttribute('aria-pressed', 'false');
    expect(due).toHaveAttribute('aria-pressed', 'false');
    expect(current).toHaveAttribute('aria-pressed', 'false');
  });

  it('loads the unfiltered list on mount', async () => {
    renderPage();
    await waitFor(() => {
      expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined);
    });
  });

  it('clicking the Overdue tile re-fetches the list scoped to overdue and marks the tile active', async () => {
    renderPage();
    const overdue = await screen.findByRole('button', { name: /Overdue/ });

    // Initial unfiltered load.
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));

    fireEvent.click(overdue);

    // Filter applied -> list re-fetched scoped to 'overdue'.
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith('overdue'));

    // Tile reflects the active filter.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Overdue/ })).toHaveAttribute('aria-pressed', 'true')
    );
  });

  it('clicking the active tile again clears the filter (re-fetches unfiltered)', async () => {
    renderPage();
    const due = await screen.findByRole('button', { name: /Due Soon/ });

    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));

    fireEvent.click(due);
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith('due'));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Due Soon/ })).toHaveAttribute('aria-pressed', 'true')
    );

    mockedApi.getEquipment.mockClear();

    // Toggle off.
    fireEvent.click(screen.getByRole('button', { name: /Due Soon/ }));
    await waitFor(() => expect(mockedApi.getEquipment).toHaveBeenCalledWith(undefined));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Due Soon/ })).toHaveAttribute('aria-pressed', 'false')
    );
  });
});
